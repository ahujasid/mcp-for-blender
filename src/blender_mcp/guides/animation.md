---
title: Animation
summary: Keyframing from Python across Blender versions, easing, cycles, NLA, cameras, and checking motion with frame strips.
---

# Animation

## Keyframes

Keyframe properties directly; it works in every version and needs no context:

```python
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 120
scene.render.fps = 24

obj.location = (0, 0, 0); obj.keyframe_insert("location", frame=1)
obj.location = (0, 0, 2); obj.keyframe_insert("location", frame=24)
obj.rotation_euler.z = 3.1416; obj.keyframe_insert("rotation_euler", index=2, frame=48)
```

Pose bones: `pb = arm.pose.bones["hand.L"]; pb.rotation_mode = "XYZ"`, then
`pb.keyframe_insert("rotation_euler", frame=f)`. Bone keys use pose-space values, not world space.

## Reaching fcurves

Blender 4.4 added slotted actions and 5.0 removed `Action.fcurves`. Use:

```python
def fcurves(obj):
    ad = obj.animation_data
    if not ad or not ad.action:
        return []
    if hasattr(ad, "action_slot"):
        from bpy_extras import anim_utils
        cb = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        return list(cb.fcurves) if cb else []
    return list(ad.action.fcurves)
```

Then set easing per key: `kp.interpolation = "BEZIER"` with `kp.easing`, or `"LINEAR"` for
mechanical motion, `"CONSTANT"` for stepped/blocking.

## Making it feel alive

- Timing and spacing matter more than poses. Ease in and out of holds; keep linear for constant
  motion (wheels, conveyors).
- Anticipation before big moves, overshoot and settle after them, follow-through on
  secondary parts (antennae, tails, cloth) offset by 2–4 frames.
- Arcs: limbs and thrown objects travel on curves, not straight lines.
- Loops: make the first and last key identical and add a Cycles modifier
  (`fc.modifiers.new("CYCLES")`), or set the frame range to exclude the duplicate last frame.
- Procedural motion: drivers (`obj.driver_add("rotation_euler", 2).driver.expression = "frame * 0.1"`)
  or noise modifiers on fcurves. Safe mode may block drivers; keyframes always work.

## Organizing

- Push finished actions to NLA strips (`track = ad.nla_tracks.new(); track.strips.new(name, start, action)`)
  to layer or sequence clips (walk, then wave).
- Name actions `Character_Walk`, `Door_Open`; game engines import them by name.
- Cameras: animate the camera or a parent empty; use a Track To constraint on a target empty for
  smooth follow shots. Keep focal-length changes slow.

## Checking motion

A single screenshot can't show motion. Use `look(mode="frames")` to see a strip of evenly spaced
frames (or pass `frames=[...]` for specific ones, `view="camera"` to see them through the camera).
Check for popping, interpenetration, feet sliding on the ground, and the loop seam.
