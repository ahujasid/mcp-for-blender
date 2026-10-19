---
title: Rigging
summary: Armatures from Python, Rigify for characters, skinning with automatic weights and how to fix it, and verifying deformation.
---

# Rigging

## Before rigging

- The mesh must have applied scale and rotation, sit at the origin on the ground, and face -Y
  (Blender's front). Rigify and auto-weights assume this.
- Merge duplicate vertices (`bmesh.ops.remove_doubles`) and remove loose parts. Generated meshes
  are often many disconnected islands, which makes automatic weights fail.
- Characters rig best in A-pose or T-pose with a bit of bend at elbows and knees.

## Humanoids: Rigify

```python
import addon_utils
addon_utils.enable("rigify", default_set=True)   # ships with Blender
bpy.ops.object.armature_human_metarig_add()     # needs a 3D viewport override
metarig = bpy.context.active_object
```

1. Scale and move the metarig to fit the mesh (edit bones: `metarig.data.edit_bones["spine"].head`),
   aligning joints with the mesh's joints. Check against the mesh with `look(mode="angles", shading="xray", views=["front","right"])`.
2. Generate: with the metarig active, `bpy.ops.pose.rigify_generate()`. The result is `rig`
   (or `RIG-<name>`).
3. Skin to the generated rig, not the metarig.

## Simple or non-humanoid rigs

Build bones directly in edit mode:

```python
arm_data = bpy.data.armatures.new("Rig"); rig = bpy.data.objects.new("Rig", arm_data)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
root = arm_data.edit_bones.new("root"); root.head = (0, 0, 0); root.tail = (0, 0.3, 0)
b = arm_data.edit_bones.new("arm"); b.head = (0, 0, 1); b.tail = (0, 0, 2); b.parent = root
bpy.ops.object.mode_set(mode="OBJECT")
```

Mechanical things (doors, wheels, robots) usually don't need skinning: parent rigid parts to bones
(`part.parent = rig; part.parent_type = "BONE"; part.parent_bone = "arm"`), which never deforms badly.

## Skinning

Automatic weights: select the mesh, make the armature active, then

```python
bpy.ops.object.parent_set(type="ARMATURE_AUTO")   # needs selection + active set
```

If Blender reports "Bone Heat Weighting: failed to find solution for one or more bones":

- Merge by distance and remove interior faces, then retry.
- Or skin a voxel-remeshed copy, then transfer weights to the original with a Data Transfer
  modifier (vertex groups, nearest face interpolated) and apply it.
- Scale the whole thing up 10x, skin, scale back down — heat weighting struggles at tiny scales.

## Verify

`get_scene_info(fields=["weights"], query="Body")` reports vertices no deform bone moves and deform
bones with no vertex group — both should be zero for a skinned character. Then pose it and look:

```python
pb = rig.pose.bones["upper_arm_fk.L"]; pb.rotation_mode = "XYZ"; pb.rotation_euler.x = 1.2
```

`look(mode="angles", target=["Body"])` and check elbows, shoulders, knees and hips for collapsing
or stretching. Reset the pose afterwards (`for pb in rig.pose.bones: pb.matrix_basis.identity()`).
