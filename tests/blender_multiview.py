"""Native smoke test in a disposable GUI session (not pytest or blender -b).

Run: xvfb-run -a blender --factory-startup --python tests/blender_multiview.py
"""
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import traceback

import bpy
import numpy as np


def check():
    spec = importlib.util.spec_from_file_location('multiview_addon', Path(__file__).resolve().parents[1] / 'addon.py')
    addon = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = addon
    spec.loader.exec_module(addon)
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for name, location, scale in [('Body', (0, 0, 1), (3, 2, 2)),
                                  ('RearPeg', (1.2, 1.3, 1.5), (.2, .8, .3)),
                                  ('Foot', (-1.2, -.7, -.25), (.3, .3, .5))]:
        bpy.ops.mesh.primitive_cube_add(size=1, location=location)
        bpy.context.object.name, bpy.context.object.scale = name, scale
    bpy.ops.object.select_all(action='SELECT')

    def snapshot():
        space = next(a.spaces.active for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
        return (len(bpy.data.objects), len(bpy.data.cameras),
                tuple(map(tuple, space.region_3d.view_matrix)),
                tuple(map(tuple, space.region_3d.window_matrix)),
                space.shading.type, space.overlay.show_overlays, space.show_gizmo,
                [(o.name, o.hide_get(), o.select_get()) for o in bpy.context.scene.objects],
                bpy.context.view_layer.objects.active)

    before = snapshot()
    result = addon._mv_capture({})
    assert (result['width'], result['height']) == (1536, 1536)
    assert len(result['views']) == 9 and snapshot() == before
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'montage.png'
        path.write_bytes(base64.b64decode(result['image_base64']))
        image = bpy.data.images.load(str(path))
        pixels = np.asarray(image.pixels[:]).reshape(1536, 1536, 4)[::-1]
        # Exclude badges: detect repeated screenshots masquerading as new angles.
        crops = {np.round(pixels[r*512+40:(r+1)*512, c*512+40:(c+1)*512, :3], 3).tobytes()
                 for r in range(3) for c in range(3)}
        bpy.data.images.remove(image)
        assert len(crops) >= 6
    addon._mv_capture({'views': [{'view': 'back', 'target': ['RearPeg'], 'isolate': True}, 'front']})
    assert snapshot() == before

    badge = addon._mv_badge
    def fail(*args):
        raise RuntimeError('injected capture failure')
    addon._mv_badge = fail
    try:
        addon._mv_capture({'target': ['Body'], 'isolate': True, 'shading': 'wireframe'})
        raise AssertionError('expected capture failure')
    except RuntimeError as exc:
        assert 'injected' in str(exc)
    finally:
        addon._mv_badge = badge
    assert snapshot() == before

    server = addon.BlenderMCPServer()
    edit = "bpy.data.objects['Body'].location.z += 0.125"
    assert 'inspection' in server.execute_code(edit, inspect={})
    assert 'inspection_error' in server.execute_code(edit, inspect={'target': ['Missing']})
    try:
        server.execute_code(edit, inspect={'views': []})
        raise AssertionError('expected invalid options')
    except ValueError:
        pass
    assert bpy.data.objects['Body'].location.z == 1.25


def run():
    try:
        check()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    print('Multi-view native checks passed', flush=True)
    bpy.ops.wm.quit_blender()


if __name__ == '__main__':
    if bpy.app.background:
        raise SystemExit('Viewport capture needs GUI Blender, not -b')
    bpy.app.timers.register(run, first_interval=1.0)
