import ast
import traceback
from pathlib import Path
from types import SimpleNamespace, MethodType
import bpy

root = Path(__file__).resolve().parents[1]
folder = root / 'test-artifacts'
folder.mkdir(exist_ok=True)
bpy.context.preferences.filepaths.temporary_directory = str(folder)

def test_preview():
    try:
        tree = ast.parse((root / 'addon.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BlenderMCPServer')
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                   and n.name in {'get_viewport_screenshot', 'preview_target'}]
        namespace = {'bpy': bpy, 'traceback': traceback}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<preview-methods>', 'exec'), namespace)
        host = SimpleNamespace()
        area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')
        region = area.spaces.active.region_3d
        def snapshot():
            return (tuple(region.view_location), tuple(region.view_rotation), region.view_distance,
                    region.view_perspective, area.spaces.active.shading.type,
                    area.spaces.active.overlay.show_overlays)
        before = snapshot()
        host.get_viewport_screenshot = lambda **kw: {'error': 'injected capture failure'}
        try:
            namespace['preview_target'](host, 'Cube', 'unused.png')
            raise AssertionError('failure expected')
        except RuntimeError:
            pass
        assert before == snapshot()
        host.get_viewport_screenshot = MethodType(namespace['get_viewport_screenshot'], host)
        for view in ('front', 'back', 'left', 'right', 'top', 'three_quarter'):
            result = namespace['preview_target'](host, 'Cube', str(folder / (view + '.png')), view, 512)
            assert result['width'] > 0 and result['height'] > 0
            assert before == snapshot()
        (folder / 'preview-result.txt').write_text('PREVIEW_SMOKE_PASSED', encoding='utf-8')
    except Exception:
        (folder / 'preview-result.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        bpy.ops.wm.quit_blender()
    return None

bpy.app.timers.register(test_preview, first_interval=1.0)
