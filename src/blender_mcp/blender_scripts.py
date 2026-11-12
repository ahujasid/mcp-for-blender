"""Scripts the server runs inside Blender through the addon's execute_code command.

Building observation tools out of execute_code instead of new addon commands
means they work with every addon already installed; only this package has to
update. Each script reads its arguments from an `ARGS` dict and prints one
RESULT_MARKER line carrying its JSON result, which `parse_result` picks out of
whatever else the script printed.
"""

import json

RESULT_MARKER = "__MCP_RESULT__"


def build(script: str, args: dict) -> str:
    """Wrap a script body in a function, so it can `return` its result early.

    The addon catches Exception around execute_code, but not SystemExit, so an
    early exit must never be spelled as one.
    """
    body = "\n".join("    " + line if line.strip() else "" for line in script.strip("\n").splitlines())
    # json.loads of a Python string literal, since JSON's true/null aren't Python.
    return (
        "import json as _json\n"
        f"ARGS = _json.loads({json.dumps(json.dumps(args))})\n"
        f"def _main():\n{body}\n"
        f"print({RESULT_MARKER!r} + _json.dumps(_main()))\n"
    )


def parse_result(output: str) -> dict:
    for line in reversed((output or "").splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    raise ValueError("Blender script finished without a result")


# One line per object, carrying only the fields the caller asked for, so the
# default listing stays a few tokens per object. Dimensions come from the
# evaluated world bounding box, which is what matters for placement (modifiers
# and parent scale included), and are only computed when asked for.
# In the order they appear on a line.
SCENE_FIELDS = ("location", "rotation", "scale", "size", "ground", "parent", "details", "materials",
                "modifiers", "animation", "hidden", "children", "topology", "weights", "settings")
SCENE_DEFAULT_FIELDS = ("location", "size", "children", "hidden")

SCENE_SUMMARY = r'''
import bpy, math
from mathutils import Vector

scene = bpy.context.scene
F = set(ARGS.get("fields") or ())
depsgraph = bpy.context.evaluated_depsgraph_get() if F & {"size", "ground"} else None
limit = max(1, min(int(ARGS.get("limit") or 20), 500))
query = (ARGS.get("query") or "").strip().lower()
root_name = ARGS.get("root")

def r(v):
    return round(float(v), 2)

def bounds(obj):
    if obj.type in {"EMPTY", "LIGHT", "CAMERA"}:
        return None
    try:
        ev = obj.evaluated_get(depsgraph)
        pts = [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    except Exception:
        return None
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi

def line(obj, depth=0):
    parts = [("  " * depth) + obj.name, obj.type.lower()]
    if "location" in F:
        loc = obj.matrix_world.translation
        parts.append(f"at ({r(loc.x)}, {r(loc.y)}, {r(loc.z)})")
    if "rotation" in F:
        e = obj.matrix_world.to_euler()
        parts.append(f"rot ({round(math.degrees(e.x))}, {round(math.degrees(e.y))}, {round(math.degrees(e.z))})")
    if "scale" in F:
        s = obj.matrix_world.to_scale()
        parts.append(f"scale ({r(s.x)}, {r(s.y)}, {r(s.z)})")
    b = bounds(obj) if F & {"size", "ground"} else None
    if b and "size" in F:
        parts.append(f"size {r(b[1][0] - b[0][0])}x{r(b[1][1] - b[0][1])}x{r(b[1][2] - b[0][2])}")
    if b and "ground" in F:
        if abs(b[0][2]) < 0.005:
            parts.append("on ground")
        elif b[0][2] < -0.005:
            parts.append(f"below ground by {r(-b[0][2])}")
        else:
            parts.append(f"floating {r(b[0][2])}")
    if "parent" in F and obj.parent:
        parts.append(f"parent {obj.parent.name}")
    if "details" in F:
        if obj.type == "MESH":
            parts.append(f"{len(obj.data.polygons)} faces")
        elif obj.type == "ARMATURE":
            parts.append(f"{len(obj.data.bones)} bones")
        elif obj.type == "LIGHT":
            parts.append(f"{obj.data.type.lower()} {r(obj.data.energy)}W")
    if "materials" in F:
        mats = [s.material.name for s in getattr(obj, "material_slots", []) if s.material]
        if mats:
            parts.append("mat " + ", ".join(mats[:3]) + ("..." if len(mats) > 3 else ""))
    if "modifiers" in F and obj.modifiers:
        parts.append("mods " + ", ".join(m.type.lower() for m in obj.modifiers))
    if "animation" in F and obj.animation_data and obj.animation_data.action:
        parts.append(f"anim {obj.animation_data.action.name}")
    if "hidden" in F and (obj.hide_get() or obj.hide_render):
        parts.append("hidden")
    if "children" in F and obj.children:
        parts.append(f"{len(obj.children)} children")
    if "topology" in F and obj.type == "MESH":
        parts.append(topology(obj))
    if "weights" in F and obj.type == "MESH":
        w = weights(obj)
        if w:
            parts.append(w)
    return " | ".join(parts)

def topology(obj):
    import bmesh
    if obj.mode == "EDIT":
        obj.update_from_editmode()
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        sides = [len(f.verts) for f in bm.faces]
        non_manifold = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
        boundary = sum(1 for e in bm.edges if e.is_boundary)
        loose = sum(1 for v in bm.verts if not v.link_edges)
        poles = sum(1 for v in bm.verts if len(v.link_edges) not in (0, 2, 4) and not v.is_boundary)
    finally:
        bm.free()
    return (f"{sides.count(4)} quads, {sides.count(3)} tris, {sum(1 for n in sides if n > 4)} ngons; "
            f"{non_manifold} non-manifold, {boundary} boundary edges, {loose} loose verts, {poles} poles")

def weights(obj):
    arm = next((m.object for m in obj.modifiers if m.type == "ARMATURE" and m.object), None)
    if arm is None:
        return None
    deform = {b.name for b in arm.data.bones if b.use_deform}
    groups = {g.index for g in obj.vertex_groups if g.name in deform}
    unweighted = sum(1 for v in obj.data.vertices if not any(g.group in groups and g.weight > 0 for g in v.groups))
    missing = sorted(deform - {g.name for g in obj.vertex_groups})
    return (f"{unweighted} of {len(obj.data.vertices)} verts unweighted by {arm.name}"
            + (f"; deform bones with no group: {', '.join(missing[:10])}" + ("..." if len(missing) > 10 else "") if missing else ""))

lines = []
total = 0
if root_name:
    root = bpy.data.objects.get(root_name)
    if root is None:
        return {"error": f"No object named {root_name!r}"}
    def walk(obj, depth):
        nonlocal total
        total += 1
        if len(lines) < limit:
            lines.append(line(obj, depth))
        for child in obj.children:
            walk(child, depth + 1)
    walk(root, 0)
else:
    if query:
        objs = [o for o in scene.objects if query in o.name.lower()]
    else:
        objs = [o for o in scene.objects if o.parent is None]
    total = len(objs)
    lines = [line(o) for o in objs[:limit]]

counts = {}
for o in scene.objects:
    counts[o.type.lower()] = counts.get(o.type.lower(), 0) + 1

active = bpy.context.view_layer.objects.active
selected = [o.name for o in bpy.context.selected_objects]
header = {
    "scene": scene.name,
    "object_counts": counts,
    "selected": selected[:5],
    "selected_count": len(selected),
    "active": active.name if active else None,
    "mode": bpy.context.mode,
}
if "settings" in F:
    world = scene.world
    hdri = None
    if world and world.use_nodes:
        for n in world.node_tree.nodes:
            if n.type == "TEX_ENVIRONMENT" and n.image:
                hdri = n.image.name
    header["settings"] = {
        "file": bpy.data.filepath or "(unsaved)",
        "engine": scene.render.engine,
        "frames": [scene.frame_start, scene.frame_end, scene.frame_current],
        "fps": scene.render.fps,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "camera": scene.camera.name if scene.camera else None,
        "world_hdri": hdri,
        "unit_scale": scene.unit_settings.scale_length,
    }
return {"header": header, "lines": lines, "total": total, "shown": len(lines)}
'''


# Renders the 3D viewport offscreen with chosen camera matrices and shading,
# tiles the results into one PNG, and restores every setting it touched. The
# offscreen path matches the addon's own screenshot, which works while the
# Blender window is in the background.
LOOK = r'''
import bpy, math
import gpu
import numpy as np
from mathutils import Vector, Matrix

scene = bpy.context.scene
depsgraph = bpy.context.evaluated_depsgraph_get()
mode = ARGS["mode"]
max_size = int(ARGS.get("max_size") or 768)

if mode == "image":
    # An image in the file ("Render Result", a texture) or on disk. Float images and
    # render results go through save_render so the scene's view transform applies,
    # as it would to the final render; byte images are shown as they are.
    import os
    ref = ARGS.get("image") or ""
    src = bpy.data.images.get(ref)
    temp = []  # images this look created, removed at the end
    if src is None:
        path = bpy.path.abspath(ref)
        if not os.path.isfile(path):
            return {"error": f"No image named {ref!r} in the file and no file at {path!r}"}
        try:
            src = bpy.data.images.load(path, check_existing=False)
        except Exception as e:
            return {"error": f"Couldn't load {path!r} as an image: {e}"}
        temp.append(src)
    out = ARGS["filepath"]
    settings = scene.render.image_settings
    saved_settings = [(a, getattr(settings, a)) for a in ("file_format", "color_mode", "color_depth")]
    try:
        if src.type == "RENDER_RESULT" or src.is_float:
            settings.file_format = "PNG"
            settings.color_mode = "RGBA"
            settings.color_depth = "8"
            try:
                src.save_render(out, scene=scene)
            except Exception as e:
                return {"error": f"Couldn't read {src.name!r}" + (" (nothing rendered yet?)" if src.type == "RENDER_RESULT" else "") + f": {e}"}
            img = bpy.data.images.load(out, check_existing=False)
            temp.append(img)
        elif src in temp:
            img = src
        else:
            img = src.copy()  # never resize the user's own image
            temp.append(img)
        w0, h0 = img.size
        if not w0 or not h0:
            return {"error": f"{src.name!r} has no pixels"}
        s = min(1.0, max_size / max(w0, h0))
        if s < 1.0:
            img.scale(max(1, int(w0 * s)), max(1, int(h0 * s)))
        img.filepath_raw = out
        img.file_format = "PNG"
        img.save()
        w, h = img.size
    finally:
        for attr, value in saved_settings:
            try:
                setattr(settings, attr, value)
            except Exception:
                pass
        for im in temp:
            try:
                bpy.data.images.remove(im)
            except Exception:
                pass
    return {"mode": "image", "image": ref, "original_size": [w0, h0], "width": w, "height": h}

area = space = region = None
for a in bpy.context.screen.areas:
    if a.type == "VIEW_3D":
        area, space = a, a.spaces.active
        region = next((rg for rg in a.regions if rg.type == "WINDOW"), None)
        break
if region is None:
    return {"error": "No 3D viewport is open in Blender"}

names = ARGS.get("target") or []
if names:
    missing = [n for n in names if n not in bpy.data.objects]
    if missing:
        return {"error": "No object named " + ", ".join(repr(m) for m in missing)}
    targets = []
    def add(o):
        if o not in targets:
            targets.append(o)
            for c in o.children:
                add(c)
    for n in names:
        add(bpy.data.objects[n])
else:
    targets = [o for o in scene.objects if o.visible_get() and o.type not in {"CAMERA", "LIGHT"}]

def world_points(objs):
    pts = []
    for o in objs:
        if o.type in {"EMPTY"} and not o.children:
            pts.append(o.matrix_world.translation.copy())
            continue
        try:
            ev = o.evaluated_get(depsgraph)
            pts.extend(ev.matrix_world @ Vector(c) for c in ev.bound_box)
        except Exception:
            pts.append(o.matrix_world.translation.copy())
    return pts

pts = world_points(targets) or [Vector((0, 0, 0))]
lo = Vector([min(p[i] for p in pts) for i in range(3)])
hi = Vector([max(p[i] for p in pts) for i in range(3)])
center = (lo + hi) / 2
radius = max((hi - lo).length / 2, 0.05)

FOV = math.radians(35)

def perspective(aspect, near, far):
    f = 1 / math.tan(FOV / 2)
    return Matrix((
        (f / aspect, 0, 0, 0),
        (0, f, 0, 0),
        (0, 0, (far + near) / (near - far), 2 * far * near / (near - far)),
        (0, 0, -1, 0),
    ))

def orbit(direction, aspect):
    direction = Vector(direction).normalized()
    fit = FOV / 2 if aspect >= 1 else math.atan(math.tan(FOV / 2) * aspect)
    dist = float(ARGS.get("distance") or 0) or radius / math.sin(fit) * 1.1
    eye = center + direction * dist
    # The camera looks down its local -Z with local Y up; to_track_quat keeps
    # that Y as close to world Z as the direction allows, so the horizon stays level.
    rot = (center - eye).to_track_quat("-Z", "Y").to_matrix().to_4x4()
    view = (Matrix.Translation(eye) @ rot).inverted()
    return view, perspective(aspect, max(dist - radius * 3, dist * 0.01), dist + radius * 3)

ANGLES = {
    "front": (0, -1, 0), "back": (0, 1, 0), "right": (1, 0, 0), "left": (-1, 0, 0),
    # Top is tilted a hair toward -Y so "up" in the image is +Y, as in Blender's top view.
    "top": (0, -0.001, 1), "three_quarter": (1, -1, 0.7),
}

def direction_of(v):
    """A named angle, or any [x, y, z] direction from the target towards the eye."""
    if isinstance(v, str):
        return ANGLES.get(v)
    if isinstance(v, (list, tuple)) and len(v) == 3 and any(v):
        return tuple(float(c) for c in v)
    return None

def label(v):
    return v if isinstance(v, str) else "(" + ", ".join(f"{float(c):g}" for c in v) + ")"

def current_view():
    r3d = space.region_3d
    return r3d.view_matrix.copy(), r3d.window_matrix.copy()

def camera_view(w, h):
    cam = scene.camera
    if cam is None:
        return None
    win = cam.calc_matrix_camera(depsgraph, x=w, y=h,
                                 scale_x=scene.render.pixel_aspect_x, scale_y=scene.render.pixel_aspect_y)
    return cam.matrix_world.inverted(), win

def draw(view, win, w, h):
    off = gpu.types.GPUOffScreen(w, h)
    try:
        off.draw_view3d(scene, bpy.context.view_layer, space, region, view, win,
                        do_color_management=True)
        buf = off.texture_color.read()
    finally:
        off.free()
    buf.dimensions = w * h * 4
    return np.asarray(buf, dtype=np.float32).reshape(h, w, 4) / 255.0

def sheet(tiles, cols):
    h, w = tiles[0].shape[:2]
    rows = math.ceil(len(tiles) / cols)
    gap = 4
    out = np.ones((rows * h + (rows - 1) * gap, cols * w + (cols - 1) * gap, 4), dtype=np.float32)
    out[..., :3] = 0.08
    # Blender images are stored bottom row first, so the first tile goes top-left.
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y = (rows - 1 - r) * (h + gap)
        x = c * (w + gap)
        out[y:y + h, x:x + w] = t
    return out

# Settings each mode changes, restored afterwards.
shading, overlay = space.shading, space.overlay
saved = []
def set_attr(obj, attr, value):
    if not hasattr(obj, attr):
        return
    saved.append((obj, attr, getattr(obj, attr)))
    try:
        setattr(obj, attr, value)
    except Exception:
        saved.pop()

requested = ARGS.get("shading")
shading_types = {"solid": "SOLID", "material": "MATERIAL", "rendered": "RENDERED", "wireframe": "SOLID", "xray": "SOLID"}
if requested:
    set_attr(shading, "type", shading_types[requested])

info = {"mode": mode, "targets": len(targets), "center": [round(v, 2) for v in center],
        "size": [round(v, 2) for v in (hi - lo)]}

if mode != "viewport":
    # The 3D cursor, light and camera gizmos and parent lines sit across generated views
    # and read as part of the scene.
    set_attr(overlay, "show_cursor", False)
    set_attr(overlay, "show_extras", False)
    set_attr(overlay, "show_relationship_lines", False)
    if mode == "camera" or ARGS.get("view") == "camera" or shading.type in {"RENDERED", "MATERIAL"}:
        # Grid, light and camera gizmos aren't part of what's being judged.
        set_attr(overlay, "show_overlays", False)
    engine = scene.render.engine
    if shading.type == "RENDERED" and "EEVEE" not in engine and engine != "BLENDER_WORKBENCH":
        # Progressive engines like Cycles draw nothing into an offscreen view.
        return {"error": f"Rendered shading can't be captured with {engine}. Render and look at "
                         "image=\"Render Result\", use material shading, or switch the engine."}
frame_before = scene.frame_current

try:
    if requested == "wireframe":
        # Edges over a plain matcap surface: topology and form in one picture.
        set_attr(shading, "light", "MATCAP")
        set_attr(shading, "color_type", "SINGLE")
        set_attr(overlay, "show_overlays", True)
        set_attr(overlay, "show_wireframes", True)
        set_attr(overlay, "wireframe_threshold", 1.0)
    elif requested == "xray":
        set_attr(shading, "show_xray", True)
        set_attr(shading, "xray_alpha", 0.35)
        set_attr(overlay, "show_overlays", True)
        set_attr(overlay, "show_bones", True)
        rigs = {o for o in targets if o.type == "ARMATURE"}
        rigs |= {m.object for o in targets for m in getattr(o, "modifiers", [])
                 if m.type == "ARMATURE" and m.object}
        for o in rigs:
            set_attr(o, "show_in_front", True)

    if mode == "viewport":
        w, h = region.width, region.height
        s = min(1.0, max_size / max(w, h))
        w, h = max(1, int(w * s)), max(1, int(h * s))
        view, win = current_view()
        image = draw(view, win, w, h)
    elif mode == "camera":
        rx, ry = scene.render.resolution_x, scene.render.resolution_y
        s = max_size / max(rx, ry)
        w, h = max(1, int(rx * s)), max(1, int(ry * s))
        cv = camera_view(w, h)
        if cv is None:
            return {"error": "The scene has no camera. Add one or use mode='angles'."}
        info["camera"] = scene.camera.name
        image = draw(cv[0], cv[1], w, h)
    elif mode == "frames":
        start, end = scene.frame_start, scene.frame_end
        frames = ARGS.get("frames") or []
        if not frames:
            n = max(2, min(int(ARGS.get("frame_count") or 6), 12))
            frames = sorted({round(start + (end - start) * i / (n - 1)) for i in range(n)})
        frames = frames[:12]
        cols = 3 if len(frames) > 4 else 2
        w = max(64, max_size // cols)
        h = int(w * 0.75)
        use_camera = scene.camera is not None and ARGS.get("view") == "camera"
        tiles = []
        for f in frames:
            scene.frame_set(int(f))
            if use_camera:
                view, win = camera_view(w, h)
            elif direction_of(ARGS.get("view")):
                view, win = orbit(direction_of(ARGS["view"]), w / h)
            else:
                view, win = current_view()
            tiles.append(draw(view, win, w, h))
        info["frames"] = [int(f) for f in frames]
        image = sheet(tiles, cols)
    else:
        views = ARGS.get("views") or ["front", "right", "top", "three_quarter"]
        views = [v for v in views if direction_of(v)][:6] or ["three_quarter"]
        cols = 1 if len(views) == 1 else (3 if len(views) > 4 else 2)
        w = max(64, max_size // cols)
        h = w
        tiles = [draw(*orbit(direction_of(v), 1.0), w, h) for v in views]
        info["views"] = [label(v) for v in views]
        image = sheet(tiles, cols)
finally:
    if scene.frame_current != frame_before:
        scene.frame_set(frame_before)
    for obj, attr, value in reversed(saved):
        try:
            setattr(obj, attr, value)
        except Exception:
            pass

h, w = image.shape[:2]
img = bpy.data.images.new("mcp_look", w, h, alpha=True)
try:
    img.pixels.foreach_set(image.ravel())
    img.filepath_raw = ARGS["filepath"]
    img.file_format = "PNG"
    img.save()
finally:
    bpy.data.images.remove(img)
info["width"], info["height"] = w, h
return info
'''


# Bounding box of freshly imported objects, so a generation or import result
# says how big the thing is and whether it sits on the ground.
BOUNDS = r'''
import bpy
from mathutils import Vector
dg = bpy.context.evaluated_depsgraph_get()
out = []
for name in ARGS["names"]:
    o = bpy.data.objects.get(name)
    if o is None:
        continue
    objs = [o] + list(o.children_recursive)
    pts = []
    for x in objs:
        if x.type in {"MESH", "CURVE", "SURFACE", "FONT", "META", "CURVES", "POINTCLOUD", "VOLUME"}:
            ev = x.evaluated_get(dg)
            pts.extend(ev.matrix_world @ Vector(c) for c in ev.bound_box)
    if not pts:
        continue
    lo = [round(min(p[i] for p in pts), 3) for i in range(3)]
    hi = [round(max(p[i] for p in pts), 3) for i in range(3)]
    out.append({"name": o.name, "world_bounding_box": [lo, hi],
                "size": [round(hi[i] - lo[i], 3) for i in range(3)]})
return out
'''
