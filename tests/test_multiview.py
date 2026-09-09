"""Camera math, PNG, execution contracts and real MCP serialization (no Blender)."""
import ast
import asyncio
import base64
import io
import json
import types
from contextlib import redirect_stdout
from typing import Any, Dict, List
from unittest.mock import Mock

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP, Context
from PIL import Image

from conftest import ROOT_ADDON, REPO_ROOT


def load_nodes(path, select, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree) if select(node)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def load_helpers():
    return load_nodes(ROOT_ADDON, lambda n:
        isinstance(n, ast.Import) and any(a.asname and a.asname.startswith('_mv_') for a in n.names)
        or isinstance(n, ast.FunctionDef) and n.name.startswith('_mv_')
        or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('_MV_') for t in n.targets), {})


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


def load_execute(capture):
    bpy = types.SimpleNamespace(counter=0)
    module = load_nodes(ROOT_ADDON,
        lambda n: isinstance(n, ast.FunctionDef) and n.name == 'execute_code',
        {'_mv_options': MV._mv_options, '_mv_capture': capture, 'bpy': bpy,
         'io': io, 'redirect_stdout': redirect_stdout})
    return module.execute_code, bpy


@pytest.mark.parametrize('inspect', [None, {}])
def test_execute_once_and_optional_capture(inspect):
    capture = Mock(return_value={'image_base64': 'image'})
    execute, bpy = load_execute(capture)
    result = execute(None, 'bpy.counter += 1\nprint("done")', inspect=inspect)
    assert bpy.counter == 1 and result['executed'] and result['result'] == 'done\n'
    if inspect is None:
        assert result == {'executed': True, 'result': 'done\n'}
        capture.assert_not_called()
    else:
        assert result['inspection'] == capture.return_value
        capture.assert_called_once_with({})


def test_bad_options_do_not_execute():
    capture = Mock()
    execute, bpy = load_execute(capture)
    with pytest.raises(ValueError):
        execute(None, 'bpy.counter += 1', inspect={'views': []})
    assert bpy.counter == 0
    capture.assert_not_called()


def test_inspection_failure_preserves_success_without_replay():
    capture = Mock(side_effect=RuntimeError('GPU context lost'))
    execute, bpy = load_execute(capture)
    result = execute(None, 'bpy.counter += 1\nprint("applied")', inspect={})
    assert bpy.counter == 1 and result['executed']
    assert result['inspection_error'] == 'GPU context lost'
    assert result['result'] == 'applied\n'
    capture.assert_called_once()


def test_execution_failure_does_not_capture():
    capture = Mock()
    execute, bpy = load_execute(capture)
    with pytest.raises(Exception, match='Code execution error'):
        execute(None, 'bpy.counter += 1\nraise ValueError("bad")', inspect={})
    assert bpy.counter == 1
    capture.assert_not_called()


@pytest.fixture
def server():
    connection = Mock()
    mcp = FastMCP('multiview-test')
    module = load_nodes(REPO_ROOT / 'src/blender_mcp/server.py',
        lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in
        {'_inspection_content', 'get_viewport_montage', 'execute_blender_code'},
        {'mcp': mcp, 'Context': Context, 'json': json, 'base64': base64, 'Any': Any, 'Dict': Dict, 'List': List,
         'get_blender_connection': lambda: connection, 'logger': Mock(),
         'trajectory_tool': lambda *a, **kw: lambda fn: fn, 'safe_mode_enabled': lambda: False})
    return mcp, connection, module


@pytest.mark.parametrize('tool', ['get_viewport_montage', 'execute_blender_code'])
def test_sdk_schema_and_native_image_without_base64_in_text(server, tool):
    mcp, connection, _ = server
    png = MV._mv_png(np.full((32, 32, 4), 255, dtype=np.uint8))
    inspection = {'image_base64': base64.b64encode(png).decode(), 'width': 32, 'height': 32,
                  'rows': 1, 'columns': 1, 'views': [{'view': 'front', 'target': ['Body']}]}
    editing = tool == 'execute_blender_code'
    connection.send_command.return_value = {'result': 'done', 'inspection': inspection} if editing else inspection

    async def check():
        definitions = {t.name: t for t in await mcp.list_tools()}
        assert 'inspect' in definitions['execute_blender_code'].inputSchema['properties']
        assert 'ctx' not in definitions[tool].inputSchema['properties']
        result = await mcp.call_tool(tool, {'code': 'pass', 'inspect': {}} if editing else {})
        content = result[0] if isinstance(result, tuple) else result
        assert [c.type for c in content] == (['text'] if editing else []) + ['text', 'image']
        assert base64.b64decode(content[-1].data) == png and content[-1].mimeType == 'image/png'
        assert all('image_base64' not in c.text and inspection['image_base64'] not in c.text
                   for c in content if c.type == 'text')
        assert json.loads(content[-2].text)['size'] == [32, 32]
    asyncio.run(check())


@pytest.mark.parametrize('payload', [None, {'error': 'unavailable'}, {'image_base64': 'not base64'},
                                    {'image_base64': base64.b64encode(b'not png').decode()}])
def test_invalid_image_payload(server, payload):
    with pytest.raises(ValueError):
        server[2]._inspection_content(payload)


@pytest.mark.parametrize('extra', [{}, {'inspection_error': 'GPU context lost'}, {'inspection': {}}])
def test_server_keeps_applied_edit_when_inspection_fails(server, extra):
    _, connection, module = server
    connection.send_command.return_value = {'result': 'done', **extra}
    result = asyncio.run(module.execute_blender_code(None, 'pass', inspect={} if extra else None))
    assert result.startswith('Code executed successfully: done')
    if extra:
        assert 'retry inspection only' in result
    else:
        assert result == 'Code executed successfully: done'
    connection.send_command.assert_called_once_with('execute_code', {'code': 'pass', **({'inspect': {}} if extra else {})})
