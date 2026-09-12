import importlib.util
import os
import sys
import traceback
from pathlib import Path
import bpy

root = Path(__file__).resolve().parents[1]
folder = root / 'test-artifacts' / 'bridge'
folder.mkdir(parents=True, exist_ok=True)


def start():
    try:
        spec = importlib.util.spec_from_file_location('blender_quality_fixture', root / 'addon.py')
        addon = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = addon
        spec.loader.exec_module(addon)
        addon.register()
        bpy.context.preferences.filepaths.temporary_directory = str(folder)
        bpy.context.scene.render.fps = 60
        bpy.context.scene.blendermcp_use_polyhaven = False
        bpy.context.scene.blendermcp_use_hyper3d = False
        bpy.context.scene.blendermcp_use_sketchfab = False
        cube = bpy.data.objects['Cube']
        cube.location = (0, 0, 0)
        cube.keyframe_insert(data_path='location', frame=1)
        cube.location = (5, 0, -1)
        cube.keyframe_insert(data_path='location', frame=2)
        bpy.context.scene.frame_set(1)
        bpy.ops.object.armature_add()
        rig = bpy.context.object
        rig.name = 'TestRig'
        bone = rig.pose.bones['Bone']
        bone.rotation_mode = 'XYZ'
        bone.rotation_euler = (0, 0, 0)
        bone.keyframe_insert(data_path='rotation_euler', frame=1)
        bone.rotation_euler = (0, 0, 0.8)
        bone.keyframe_insert(data_path='rotation_euler', frame=2)
        bpy.ops.mesh.primitive_cube_add(size=0.2)
        prop = bpy.context.object
        prop.name = 'HeldProp'
        prop.parent = rig
        prop.parent_type = 'BONE'
        prop.parent_bone = 'Bone'
        prop.location = (0.1, 0, 0)
        bpy.ops.mesh.primitive_cube_add(size=0.5)
        skin = bpy.context.object
        skin.name = 'SkinnedMesh'
        skin.parent = rig
        weights = skin.vertex_groups.new(name='Bone')
        weights.add(list(range(len(skin.data.vertices))), 1.0, 'REPLACE')
        modifier = skin.modifiers.new('Deform', 'ARMATURE')
        modifier.object = rig
        bpy.context.scene.frame_set(1)
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        cube.select_set(True)
        bpy.context.view_layer.objects.active = cube
        server = addon.BlenderMCPServer(host='127.0.0.1', port=int(os.environ['BLENDER_MCP_TEST_PORT']))
        server.start()
        if not server.running:
            raise RuntimeError('Fixture bridge failed to start')
        bpy._quality_test_server = server
        (folder / 'ready.txt').write_text('ready', encoding='utf-8')
        def stop():
            if (folder / 'stop.txt').exists():
                server.stop()
                bpy.ops.wm.quit_blender()
                return None
            return 0.5
        bpy.app.timers.register(stop, persistent=True)
    except Exception:
        (folder / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(start, first_interval=1.0)
