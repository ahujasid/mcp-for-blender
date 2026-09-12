import ast
from pathlib import Path
from types import SimpleNamespace, MethodType
import bpy

root = Path(__file__).resolve().parents[1]
folder = root / 'test-artifacts' / 'advanced'
folder.mkdir(parents=True, exist_ok=True)
tree = ast.parse((root / 'addon.py').read_text(encoding='utf-8'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BlenderMCPServer')
names = {'inspect_rig', 'inspect_motion', 'validate_animation', 'validate_deformation',
         'inspect_sculpt', 'sculpt_region', 'get_sculpt_revision', 'create_checkpoint', 'compare_reference', 'export_asset'}
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
namespace = {'bpy': bpy}
exec(compile(ast.Module(body=methods, type_ignores=[]), '<advanced-methods>', 'exec'), namespace)
host = SimpleNamespace()
for name in names:
    setattr(host, name, MethodType(namespace[name], host))

cube = bpy.data.objects['Cube']
cube.scale = (1, 1, 1)
cube.keyframe_insert(data_path='scale', frame=1)
cube.scale = (8, 1, 1)
cube.keyframe_insert(data_path='scale', frame=2)
bpy.context.scene.frame_set(7, subframe=0.5)
deform = host.validate_deformation('Cube', [1, 2], 1)
assert deform['samples'][0]['flagged_edges'] == 0
assert deform['samples'][1]['flagged_edges'] == 4
assert bpy.context.scene.frame_current == 7 and bpy.context.scene.frame_subframe == 0.5
cube.location = (0, 0, 0)
cube.keyframe_insert(data_path='location', frame=1)
cube.location = (5, 0, -1)
cube.keyframe_insert(data_path='location', frame=2)
motion = host.validate_animation('Cube', 1, 2, loop=True, contact_start=1, contact_end=2, ground_z=0)
assert {w['kind'] for w in motion['warnings']} == {'loop_position_gap', 'contact_origin_slide', 'contact_origin_below_ground'}
assert bpy.context.scene.frame_current == 7 and bpy.context.scene.frame_subframe == 0.5
bpy.ops.object.armature_add()
rig = bpy.context.object
rig.name = 'TestRig'
group = cube.vertex_groups.new(name='Bone')
group.add([0, 1, 2], 0.5, 'REPLACE')
mod = cube.modifiers.new('TestArmature', 'ARMATURE')
mod.object = rig
report = host.inspect_rig('TestRig', mesh_name='Cube')
assert report['weights']['counts']['unweighted'] == 5
assert report['weights']['counts']['not_normalized'] == 3
assert report['weights']['armature_modifier_bound']
assert report['bones'][0]['name'] == 'Bone'
cube.rotation_euler = (0, 0, 0)
cube.keyframe_insert(data_path='rotation_euler', frame=1)
cube.rotation_euler = (0, 0, 1.5)
cube.keyframe_insert(data_path='rotation_euler', frame=2)
report = host.validate_animation('Cube', 1, 2, reference_object='TestRig', reference_bone='Bone', loop=True)
assert {'attachment_rotation_changed', 'loop_rotation_gap'} <= {w['kind'] for w in report['warnings']}
assert abs(report['loop']['rotation_gap_radians'] - 1.5) < 1e-5
assert bpy.context.scene.frame_current == 7 and bpy.context.scene.frame_subframe == 0.5

bpy.ops.mesh.primitive_grid_add(x_subdivisions=3, y_subdivisions=3)
grid = bpy.context.object
grid.name = 'SculptFixture'
before = [tuple(v.co) for v in grid.data.vertices]
mask = grid.data.attributes.new('.sculpt_mask', 'FLOAT', 'POINT')
mask.data[0].value = 1
revision = host.inspect_sculpt(grid.name)['revision']
report = host.sculpt_region(grid.name, [0, 0, 0], 3, [0, 0, 0.25], expected_revision=revision, operation_id='masked-edit')
assert report['changed_vertices'] == len(grid.data.vertices) - 1
assert tuple(grid.data.vertices[0].co) == before[0]
assert len(grid.data.vertices) == len(before)
assert host.inspect_sculpt(grid.name)['vertices'] == len(before)
assert Path(report['checkpoint']['path']).is_file()
after = [tuple(v.co) for v in grid.data.vertices]
replay = host.sculpt_region(grid.name, [0, 0, 0], 3, [0, 0, 0.25], expected_revision=revision, operation_id='masked-edit')
assert replay['replayed'] and after == [tuple(v.co) for v in grid.data.vertices]
try:
    host.sculpt_region(grid.name, [0, 0, 0], 3, [0, 0, 0.25], expected_revision=revision, operation_id='stale-edit')
    raise AssertionError('Stale edit accepted')
except ValueError:
    pass
assert after == [tuple(v.co) for v in grid.data.vertices]
shared = grid.copy()
bpy.context.collection.objects.link(shared)
try:
    host.sculpt_region(grid.name, [0, 0, 0], 3, [0, 0, 1])
    raise AssertionError('Shared mesh edit accepted')
except ValueError:
    pass
bpy.data.objects.remove(shared, do_unlink=True)

for label, color in [('red', [1, 0, 0, 1]), ('blue', [0, 0, 1, 1])]:
    image = bpy.data.images.new(label, width=16, height=16)
    image.pixels.foreach_set(color * 256)
    image.filepath_raw = str(folder / (label + '.png'))
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)
for label in ('comparison', 'overlay', 'asset'):
    path = folder / (label + ('.glb' if label == 'asset' else '.png'))
    path.unlink(missing_ok=True)
images_before = len(bpy.data.images)
report = host.compare_reference(str(folder / 'red.png'), str(folder / 'blue.png'), str(folder / 'comparison.png'))
assert report['accuracy_score'] is None and len(bpy.data.images) == images_before
report = host.compare_reference(str(folder / 'red.png'), str(folder / 'blue.png'), str(folder / 'overlay.png'), True, 'overlay')
assert report['mode'] == 'overlay'
selection = [o.name for o in bpy.context.selected_objects]
report = host.export_asset([grid.name], str(folder / 'asset.glb'))
assert report['bytes'] > 0 and report['target_engine_verified'] is False
assert selection == [o.name for o in bpy.context.selected_objects]
for record in host._quality_checkpoints.values():
    Path(record['path']).unlink(missing_ok=True)
print('ADVANCED_SMOKE_PASSED')
