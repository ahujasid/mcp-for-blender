"""Run INSIDE GUI Blender, not -b. Saves actual native results and inspection images.

blender --factory-startup --python native_validation.py -- --addon /path/addon.py --out results/native
This deliberately labels capture microbenchmarks separately from agent benchmarks.
"""
import argparse
import base64
import importlib.util
import json
import os
import platform
import sys
import time
import traceback
from pathlib import Path

import bpy
import gpu
import numpy as np


def parse_args():
    ap=argparse.ArgumentParser()
    ap.add_argument('--addon',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    return ap.parse_args(sys.argv[sys.argv.index('--')+1:])


def load_addon(path):
    spec=importlib.util.spec_from_file_location('multiview_bench_addon',path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    return module


def run():
    args=parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    report={'kind':'native_blender_validation','passed':False,'checks':[],
            'blender_version':bpy.app.version_string,'platform':platform.platform(),
            'renderer':gpu.platform.renderer_get(),'vendor':gpu.platform.vendor_get(),
            'capture_trials':[],'llm_calls':0,'llm_tokens':None,'llm_cost_usd':None}
    try:
        module=load_addon(args.addon)
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        def cube(name,location,scale):
            bpy.ops.mesh.primitive_cube_add(size=1,location=location)
            obj=bpy.context.object; obj.name=name; obj.scale=scale
            return obj
        body=cube('Body',(0,0,1),(3,2,2))
        peg=cube('RearPeg',(1.2,1.3,1.5),(.2,.8,.3))
        foot=cube('Foot',(-1.2,-.7,-.25),(.3,.3,.5))
        detail=cube('Detail',(1.8,-.4,1.5),(.6,.2,.2))
        detail.rotation_euler.z=.4
        for obj in bpy.context.view_layer.objects: obj.select_set(True)
        def snapshot():
            space=next(a.spaces.active for a in bpy.context.screen.areas if a.type=='VIEW_3D')
            r=space.region_3d
            return {'objects':len(bpy.data.objects),'cameras':len(bpy.data.cameras),
                    'view':tuple(tuple(v) for v in r.view_matrix),
                    'projection':tuple(tuple(v) for v in r.window_matrix),
                    'shading':space.shading.type,'overlay':space.overlay.show_overlays,
                    'gizmo':space.show_gizmo,'hidden':[(o.name,o.hide_get()) for o in bpy.context.scene.objects],
                    'active':bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
                    'selection':sorted(o.name for o in bpy.context.selected_objects)}
        before=snapshot()
        result=module._mv_capture({})
        assert (result['width'],result['height'])==(1536,1536)
        assert len(result['views'])==9
        assert snapshot()==before, {k: (before[k], snapshot()[k]) for k in before if before[k] != snapshot()[k]}
        (args.out/'montage.png').write_bytes(base64.b64decode(result['image_base64']))
        image=bpy.data.images.load(str(args.out/'montage.png'))
        pixels=np.asarray(image.pixels[:]).reshape(1536,1536,4)[::-1]
        import hashlib
        signatures=set()
        for i in range(9):
            r,c=divmod(i,3)
            crop=pixels[r*512+40:(r+1)*512,c*512+40:(c+1)*512,:3]
            signatures.add(hashlib.sha256(np.round(crop,3).tobytes()).hexdigest())
        bpy.data.images.remove(image)
        assert len(signatures)>=6, 'Virtual cameras may have returned repeated viewports'
        report['checks'].append('default nine-view GPU capture; unique views; complete viewport/selection restoration')
        isolation=module._mv_capture({'views':[{'view':'back','target':['RearPeg'],'isolate':True},'front']})
        assert snapshot()==before, {k: (before[k], snapshot()[k]) for k in before if before[k] != snapshot()[k]}
        (args.out/'isolation.png').write_bytes(base64.b64decode(isolation['image_base64']))
        report['checks'].append('per-view target/isolation and restored hidden flags')
        saved_badge=module._mv_badge
        def fail(*args): raise RuntimeError('injected capture failure')
        module._mv_badge=fail
        try:
            module._mv_capture({'target':['Body'],'isolate':True,'shading':'wireframe'})
            raise AssertionError('expected injected failure')
        except RuntimeError as exc:
            assert 'injected' in str(exc)
        finally:
            module._mv_badge=saved_badge
        assert snapshot()==before
        report['checks'].append('failure inside capture restores all modified state')
        server=module.BlenderMCPServer()
        result=server.execute_code("bpy.data.objects['Body'].location.z += 0.125",inspect={})
        assert result['executed'] and 'inspection' in result
        assert abs(body.location.z-1.125)<1e-6
        result=server.execute_code("bpy.data.objects['Body'].location.z += 0.125",inspect={'target':['Missing']})
        assert result['executed'] and 'inspection_error' in result
        assert abs(body.location.z-1.25)<1e-6
        report['checks'].append('edit once; missing post-edit target cannot replay or erase success')
        names=['front','right','back','left','top','bottom','iso_front_right','iso_back_left']
        # Same output pixel size per view. This is capture overhead, not a claim
        # about baseline agent behavior or the old screenshot endpoint.
        for repeat in range(5):
            order=('single_view_calls','one_montage_call') if repeat%2==0 else ('one_montage_call','single_view_calls')
            for mode in order:
                start=time.perf_counter()
                if mode=='single_view_calls':
                    responses=[module._mv_capture({'views':[n],'max_size':512}) for n in names]
                else:
                    responses=[module._mv_capture({'views':names,'max_size':1536})]
                report['capture_trials'].append({'repeat':repeat,'mode':mode,
                    'elapsed_ms':(time.perf_counter()-start)*1000,'calls':len(responses),
                    'views':len(names),'png_bytes':sum(len(base64.b64decode(r['image_base64'])) for r in responses)})
        report['passed']=True
    except Exception:
        report['error']=traceback.format_exc()
    (args.out/'native.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    bpy.ops.wm.quit_blender()
    return None

if __name__=='__main__':
    if bpy.app.background:
        raise SystemExit('Use GUI Blender (optionally xvfb-run); -b cannot validate viewport capture')
    bpy.app.timers.register(run,first_interval=1.0)
