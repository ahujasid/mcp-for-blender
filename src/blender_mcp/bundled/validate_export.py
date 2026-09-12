import json
import sys
import traceback
from pathlib import Path
import bpy


def main():
    arguments = sys.argv[sys.argv.index('--') + 1:]
    source, destination, frames_file = map(Path, arguments)
    try:
        settings = json.loads(frames_file.read_text(encoding='utf-8'))
        settings = {'frames': settings} if isinstance(settings, list) else settings
        frames = settings['frames']
        bpy.context.scene.render.fps = settings.get('fps', 24)
        bpy.context.scene.render.fps_base = settings.get('fps_base', 1)
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        result = bpy.ops.import_scene.gltf(filepath=str(source))
        if 'FINISHED' not in result:
            raise RuntimeError('GLB import did not complete')
        objects = []
        for obj in sorted(bpy.context.scene.objects, key=lambda item: item.name):
            mesh = obj.data if obj.type == 'MESH' else None
            if mesh:
                mesh.calc_loop_triangles()
            objects.append({'name': obj.name, 'type': obj.type,
                            'dimensions': list(obj.dimensions),
                            'world_position': list(obj.matrix_world.translation),
                            'triangles': len(mesh.loop_triangles) if mesh else None,
                            'bones': len(obj.data.bones) if obj.type == 'ARMATURE' else None})
        if len(objects) > 1000:
            raise ValueError('Imported asset exceeds the 1000-object report limit')
        motion = []
        reference_name = settings.get('reference_object', '')
        reference_bone = settings.get('reference_bone', '')
        reference = bpy.context.scene.objects.get(reference_name) if reference_name else None
        if reference_name and reference is None:
            raise ValueError('Reference object was not preserved in the exported asset')
        if reference_bone and (reference is None or reference.type != 'ARMATURE' or reference_bone not in reference.pose.bones):
            raise ValueError('Reference bone was not preserved in the exported asset')
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            graph = bpy.context.evaluated_depsgraph_get()
            basis = None
            if reference:
                evaluated_ref = reference.evaluated_get(graph)
                basis = evaluated_ref.matrix_world.copy()
                if reference_bone:
                    basis = basis @ evaluated_ref.pose.bones[reference_bone].matrix
                basis = basis.inverted()
            samples = []
            for obj in bpy.context.scene.objects:
                matrix = obj.evaluated_get(graph).matrix_world.copy()
                relative = basis @ matrix if basis is not None else None
                samples.append({'name': obj.name, 'position': list(matrix.translation),
                                'rotation': list(matrix.to_quaternion()),
                                'reference_position': list(relative.translation) if relative is not None else None,
                                'reference_rotation': list(relative.to_quaternion()) if relative is not None else None})
            motion.append({'frame': frame, 'objects': samples})
        report = {'imported': True, 'objects': objects, 'motion': motion,
                  'actions': [action.name for action in bpy.data.actions],
                  'images': [{'name': image.name, 'has_data': image.has_data,
                              'size': list(image.size)} for image in bpy.data.images],
                  'blender_version': bpy.app.version_string, 'target_engine_verified': False,
                  'limitations': ['A Blender GLB round trip is not a target-engine test.',
                                  'Motion samples report object origins, not skinned surfaces.']}
    except Exception:
        report = {'imported': False, 'error': traceback.format_exc()}
    destination.write_text(json.dumps(report), encoding='utf-8')


if __name__ == '__main__':
    main()
