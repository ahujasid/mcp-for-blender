---
title: Level design
summary: Game levels in Blender - player metrics, blockout, modular kits, instancing, collisions, and export to game engines.
---

# Level design

## Metrics first

Agree on the player's size and movement, then build everything to them. Defaults for a
human-scale third/first-person game (adjust to the user's engine and genre):

| Thing | Size |
|---|---|
| Player capsule | 1.8 m tall, 0.6 m wide |
| Doorway | 1.2–1.5 m wide, 2.4–3 m tall (wider than real life) |
| Corridor | 3–4 m wide |
| Step | 0.2 m rise, 0.3 m run |
| Max climbable ledge | ~1 m (jump) |
| Cover (crouch / full) | 1.0 m / 2.0 m |
| Grid unit | 1 m, modules on 2 m or 4 m |

Place a scale reference (a 1.8 m capsule named `PlayerScale`) and keep it in the scene.

## Blockout

- Grey-box the whole level with simple geometry before any art: rooms, paths, heights,
  sightlines. One collection per area (`Area_Entrance`, `Area_Arena`...).
- Snap to the grid. Round positions and sizes to the module size in your scripts.
- Design the critical path, then branches, loops and secrets. Landmarks (tall, distinct shapes)
  help navigation; check they're visible from where the player needs them with
  `look(mode="angles", views=["top"])` and eye-height views.
- Top view (`look(mode="angles", views=["top"])`) is the fastest way to review flow and spacing.

## Modular kits

- Build pieces on the grid: wall 4×3 m, floor 4×4 m, corner, doorway, stairs. Origin at a corner
  or bottom-centre so pieces snap.
- Place with linked duplicates (`obj.copy()` without copying data) or collection instances, so a
  fix to one piece updates every copy and the file stays small.
- Scatter props with slight random rotation and scale; keep gameplay-critical props exact.

## Engine-ready output

- Apply transforms (scale 1, rotation 0) on every exported mesh.
- Name for the engine's conventions. Unreal: `SM_` meshes and `UCX_<mesh>` convex collision.
  Godot: `-col` / `-colonly` suffixes. Unity: separate low-poly collision meshes.
- Keep collision simple: boxes and convex hulls, not the render mesh.
- Triangle budgets depend on platform; report counts with `get_scene_info(fields=["topology"])`.
- Export with `bpy.ops.export_scene.gltf` (Godot, web, most engines) or `export_scene.fbx`
  (Unity, Unreal). Read each operator's arguments first; they change between versions.
- Lightmap UVs go in a second UV map when the engine bakes lighting.
