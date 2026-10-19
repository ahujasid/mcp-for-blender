---
title: Writing bpy that works on any Blender
summary: Version-proof scripting, API introspection, operator context, and the mistakes that break scripts on other people's machines.
---

# Writing bpy that works on any Blender

Scripts run in the user's live Blender, whose version, language and add-ons you don't know.
`get_addon_status` reports `blender_version`; check it before using anything version-specific.

## Look things up instead of guessing

Introspect with RNA before indexing sockets or setting enums:

```python
# Valid values of any enum property
[i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
# Properties of a type
[p.identifier for p in bpy.types.ShaderNodeTexSky.bl_rna.properties]
# Operator arguments
[(p.identifier, p.type) for p in bpy.ops.mesh.primitive_cube_add.get_rna_type().properties]
# A node's sockets, in the mode you intend to use
tree = bpy.data.node_groups.new("_probe", "ShaderNodeTree")
n = tree.nodes.new("ShaderNodeMix"); n.data_type = "RGBA"
print([(i, s.name, s.type) for i, s in enumerate(n.inputs) if s.enabled])
bpy.data.node_groups.remove(tree)
```

## Portability rules

- Find nodes by `type`, never by name: names are localized. `next(n for n in nodes if n.type == "BSDF_PRINCIPLED")`.
- Address sockets by name only after checking they exist in this version (Principled BSDF renamed
  many inputs in 4.0: `Emission Color`, `Subsurface Weight`, `Specular IOR Level`, `Coat Weight`,
  `Sheen Weight`, `Transmission Weight`). `sock = node.inputs.get("Emission Color") or node.inputs.get("Emission")`.
- Never hardcode enum identifiers; read them (above). `scene.render.engine` is a dynamic enum that
  RNA under-reports; read the current value, and if you must switch, assign inside
  `try/except TypeError` — the error lists every accepted value. EEVEE is `BLENDER_EEVEE_NEXT` in
  4.2–4.x and `BLENDER_EEVEE` before and after.
- Material colors live on shader node inputs. `material.diffuse_color` only drives the solid
  viewport. In 5.0+ `material.use_nodes` is always on; don't rely on toggling it.
- Animation data changed in 4.4 (slotted actions) and 5.0 removed `Action.fcurves`. Insert keys with
  `obj.keyframe_insert("location", frame=f)` (works everywhere) and reach fcurves through
  `bpy_extras.anim_utils.action_get_channelbag_for_slot(action, obj.animation_data.action_slot)`
  on 4.4+, falling back to `action.fcurves` on older versions.

## Data API over operators

Prefer `bpy.data` and object properties over `bpy.ops`: they need no context and are faster.
`bpy.data.meshes.new` + `from_pydata`, `bmesh` for editing, `obj.modifiers.new`, `obj.matrix_world`.

When you need an operator:

- Code runs from a timer, not a UI area. Operators that need a viewport need an override:
  ```python
  area = next(a for a in bpy.context.screen.areas if a.type == "VIEW_3D")
  region = next(r for r in area.regions if r.type == "WINDOW")
  with bpy.context.temp_override(area=area, region=region, active_object=obj, selected_objects=[obj]):
      bpy.ops.object.shade_smooth()
  ```
- `mode_set` needs an active, visible, selectable object. Always return to OBJECT mode at the end
  of a step, even on failure (`try/finally`).
- `obj.select_set(True)` and `bpy.context.view_layer.objects.active = obj` before ops that act on
  the selection.

## Working style

- Small steps. Run a chunk and `print` what you need to know.
- Name everything you create; later steps and the user refer to it by name.
- Keep the scene organized: one collection per logical group (`bpy.data.collections.new`, link
  to `scene.collection`).
- Apply scale (`obj.data.transform(Matrix.Diagonal(...))` or `transform_apply`) before rigging,
  modifiers that depend on it, or export.
- Duplicate with `obj.copy()` (shares mesh data, cheap) for repeated props; `obj.data.copy()` only
  when the copy must differ.
- Units are metres. A door is ~2.1 m tall, a table 0.75 m, a person 1.7 m.
