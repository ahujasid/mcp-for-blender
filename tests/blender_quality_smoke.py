import ast
from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]
tree = ast.parse((root / 'addon.py').read_text(encoding='utf-8'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BlenderMCPServer')
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
           and n.name in {'inspect_scene', 'validate_mesh', 'inspect_motion',
                          'create_checkpoint', 'restore_checkpoint', 'preview_target'}]
namespace = {'bpy': bpy}
exec(compile(ast.Module(body=methods, type_ignores=[]), '<quality-methods>', 'exec'), namespace)
inspect = namespace['inspect_scene']
validate = namespace['validate_mesh']
for i in range(25):
    obj = bpy.data.objects.new(f'Fixture_{i:02}', None)
    bpy.context.collection.objects.link(obj)
page = inspect(None, query='Fixture_', limit=10)
assert page['total_matches'] == 25 and page['next_offset'] == 10
assert len(inspect(None, offset=20, query='Fixture_', limit=10)['objects']) == 5
assert inspect(None, query='missing')['next_offset'] is None
for kwargs in ({'limit': 0}, {'offset': -1}, {'limit': True}):
    try:
        inspect(None, **kwargs)
        raise AssertionError('Invalid pagination accepted')
    except ValueError:
        pass
cube = bpy.data.objects['Cube']
before = [tuple(v.co) for v in cube.data.vertices]
report = validate(None, 'Cube', triangle_budget=11)
assert report['counts']['triangles'] == 12
assert report['within_triangle_budget'] is False
assert report['counts']['boundary_edges'] == 0
assert before == [tuple(v.co) for v in cube.data.vertices]
modifier = cube.modifiers.new('Subdivision test', 'SUBSURF')
modifier.levels = 1
assert validate(None, 'Cube')['counts']['triangles'] > 12
assert len(cube.data.polygons) == 6
mesh = bpy.data.meshes.new('OpenMesh')
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new('OpenTriangle', mesh)
bpy.context.collection.objects.link(obj)
assert validate(None, obj.name)['counts']['boundary_edges'] == 3
assert (root / 'addon.py').read_bytes() == (root / 'src/blender_mcp/bundled/addon.py').read_bytes()
motion = namespace['inspect_motion']
cube.location = (0, 0, 0)
cube.keyframe_insert(data_path='location', frame=1)
cube.location = (10, 0, 0)
cube.keyframe_insert(data_path='location', frame=2)
bpy.context.scene.frame_set(8, subframe=0.25)
report = motion(None, 'Cube', 1, 2, max_step_distance=2)
assert len(report['warnings']) == 1
assert report['samples'][1]['world_step_distance'] == 10
assert bpy.context.scene.frame_current == 8 and bpy.context.scene.frame_subframe == 0.25
report = motion(None, 'Cube', 1, 2, reference_object='Cube')
assert all(abs(v) < 1e-6 for row in report['samples'] for v in row['reference_position'])
try:
    motion(None, 'Cube', 1, 1000)
    raise AssertionError('Unbounded motion accepted')
except ValueError:
    pass
from types import SimpleNamespace, MethodType
host = SimpleNamespace()
host.create_checkpoint = MethodType(namespace['create_checkpoint'], host)
host.restore_checkpoint = MethodType(namespace['restore_checkpoint'], host)
original_file = bpy.data.filepath
checkpoint = host.create_checkpoint('Fixture recovery')
assert bpy.data.filepath == original_file
cube['recovery_test'] = 'changed'
restored = host.restore_checkpoint(checkpoint['checkpoint'])
assert 'recovery_test' not in bpy.data.objects['Cube']
assert Path(restored['rescue_checkpoint']['path']).is_file()
assert bpy.data.filepath == checkpoint['path']
area = next((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), None)
if bpy.app.background:
    try:
        namespace['preview_target'](host, 'Cube', 'unused.png')
        raise AssertionError('Background preview should be rejected')
    except ValueError as exc:
        assert 'interactive Blender' in str(exc)
elif area:
    region = area.spaces.active.region_3d
    before = (tuple(region.view_location), tuple(region.view_rotation), region.view_distance,
              region.view_perspective, area.spaces.active.shading.type,
              area.spaces.active.overlay.show_overlays)
    def fail_capture(**kwargs):
        return {'error': 'Injected capture failure'}
    host.get_viewport_screenshot = fail_capture
    try:
        namespace['preview_target'](host, 'Cube', 'unused.png')
        raise AssertionError('Expected capture failure')
    except RuntimeError as exc:
        assert 'Injected capture failure' in str(exc)
    after = (tuple(region.view_location), tuple(region.view_rotation), region.view_distance,
             region.view_perspective, area.spaces.active.shading.type,
             area.spaces.active.overlay.show_overlays)
    assert before == after
for record in host._quality_checkpoints.values():
    Path(record['path']).unlink(missing_ok=True)
print('QUALITY_SMOKE_PASSED')
