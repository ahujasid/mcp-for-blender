"""Pure math/encoding tests. These are NOT Blender/GPU or LLM benchmarks."""
import ast
import io
import math
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def load_helpers():
    root = Path(__file__).resolve().parents[1]
    source = root/'implementation/montage_addon.py'
    if not source.exists():
        source = root/'addon.py'
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Import) and any(a.asname and a.asname.startswith('_mv_') for a in node.names):
            nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name.startswith('_mv_'):
            nodes.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('_MV_') for t in node.targets):
            nodes.append(node)
    module = types.ModuleType('multiview_under_test')
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),module.__dict__)
    return module


MV = load_helpers()


def test_default_layout_and_view_order():
    cfg=MV._mv_options({})
    assert cfg['width']==1536 and cfg['height']==1536 and cfg['cell']==512
    assert [v['name'] for v in cfg['views']]==list(MV._MV_AUTO)
    assert all(v['shading']=='solid' and not v['isolate'] for v in cfg['views'])


@pytest.mark.parametrize('n',[1,2,3,4,5,8,9,10,15,16])
def test_layout_bound(n):
    c=MV._mv_options({'views':['front']*n,'max_size':1536})
    assert max(c['width'],c['height'])<=1536
    assert c['cell']%32==0 and c['rows']*c['columns']>=n


@pytest.mark.parametrize('bad',[
    [],True,{'views':[]},{'views':['auto']},{'views':['front']*17},
    {'max_size':True},{'max_size':512.5},{'max_size':100},{'max_size':8192},
    {'views':['nonsense']},{'views':[{}]}, {'views':[None]},
    {'views':[{'azimuth':3}]},{'views':[{'azimuth':float('nan'),'elevation':0}]},
    {'views':[{'azimuth':0,'elevation':91}]},{'views':[{'view':'front','azimuth':3}]},
    {'views':[{'view':'current','projection':'orthographic'}]},
    {'target':[]},{'target':['']},{'target':[3]},{'target':['a']*257},
    {'padding':1},{'padding':float('inf')},{'shading':'cycles'},
    {'isolate':1},{'views':[{'view':'top','isolate':'false'}]},
    {'view':['front']},{'views':[{'view':'front','typo':0}]},
])
def test_reject_bad_options(bad):
    with pytest.raises(ValueError):
        MV._mv_options(bad)


def test_custom_view_and_overrides_do_not_mutate_input():
    request={'target':['Body'],'views':[{'azimuth':90,'elevation':0,'target':['Handle'],
             'isolate':True,'shading':'wireframe'},'top']}
    cfg=MV._mv_options(request)
    assert np.allclose(cfg['views'][0]['direction'],[1,0,0])
    assert cfg['views'][0]['target']==['Handle']
    assert cfg['views'][1]['target']==['Body']
    assert cfg['views'][0]['isolate'] and not cfg['views'][1]['isolate']
    assert request['views'][1]=='top'


def project(points,view,proj):
    p=np.column_stack([points,np.ones(len(points))])
    clip=(np.asarray(proj)@np.asarray(view)@p.T).T
    return clip[:,:3]/clip[:,3:],clip[:,3]


@pytest.mark.parametrize('projection',['orthographic','perspective'])
def test_camera_contains_random_rotated_scaled_offset_bounds(projection):
    rng=np.random.default_rng(35122)
    for _ in range(1200):
        scale=10**rng.uniform(-5,5,size=3)
        corners=np.array([(x,y,z) for x in (-1,1) for y in (-1,1) for z in (-1,1)])*scale
        q,_=np.linalg.qr(rng.normal(size=(3,3)))
        points=corners@q+rng.normal(size=3)*max(scale)*20
        direction=rng.normal(size=3)
        aspect=float(10**rng.uniform(-.7,.7))
        view,proj=MV._mv_camera(points.tolist(),direction,projection,aspect=aspect)
        ndc,w=project(points,view,proj)
        assert np.all(np.isfinite(ndc))
        assert np.max(np.abs(ndc[:,:2]))<1+1e-7
        assert np.max(np.abs(ndc[:,2]))<1+1e-7
        assert np.all(w>0)
        rotation=np.array(view)[:3,:3]
        assert np.allclose(rotation@rotation.T,np.eye(3),atol=1e-9)
        assert np.linalg.det(rotation)>0.99999


@pytest.mark.parametrize('direction',list(MV._MV_VIEWS.values()))
def test_cardinal_and_polar_views(direction):
    v,p=MV._mv_camera([[-3,-2,-1],[4,3,2]],direction,'perspective')
    assert np.isfinite(np.array(v)).all()
    assert np.isfinite(np.array(p)).all()


@pytest.mark.parametrize('points',[[[0,0,0]],[[0,0,0],[0,0,2]],[[3,3,3],[3,3,3]]])
def test_degenerate_but_finite_bounds(points):
    for mode in ('perspective','orthographic'):
        v,p=MV._mv_camera(points,(0,0,1),mode)
        xyz,w=project(points,v,p)
        assert np.isfinite(xyz).all() and (w>0).all()


@pytest.mark.parametrize('points',[[],[[float('nan'),0,0]],[[1,2]],[[float('inf'),0,0]]])
def test_invalid_bounds(points):
    with pytest.raises(ValueError):
        MV._mv_camera(points,(1,0,0),'perspective')


def test_png_roundtrip_exact_colors_and_row_orientation():
    rng=np.random.default_rng(12)
    original=rng.integers(0,256,(35,69,4),dtype=np.uint8)
    decoded=np.asarray(Image.open(io.BytesIO(MV._mv_png(original))))
    assert np.array_equal(original,decoded)


def test_png_reject_wrong_dtype():
    with pytest.raises(ValueError):
        MV._mv_png(np.zeros((4,4,4),dtype=np.float32))


@pytest.mark.parametrize('n',[1,9,10,16])
def test_badge_is_visible_and_local(n):
    tile=np.full((512,512,4),127,dtype=np.uint8)
    MV._mv_badge(tile,n)
    assert np.any(tile[:40,:40,0]==255)
    assert np.any(tile[:40,:40,0]==20)
    assert np.all(tile[40:,40:]==127)


def test_azimuth_matches_documented_front_right():
    for angle,named in ((0,'front'),(90,'right'),(180,'back'),(270,'left')):
        c=MV._mv_options({'views':[{'azimuth':angle,'elevation':0}]})
        assert np.allclose(c['views'][0]['direction'],MV._MV_VIEWS[named],atol=1e-10)
