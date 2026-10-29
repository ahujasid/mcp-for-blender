"""A beveled curve's reported size is the evaluated mesh, not its control cage.

The cage bound_box of a small beveled poly curve is about 2 m, which makes
look's auto-framing pull the camera way back. Scene summary and look both
call world_bounds, so this checks the size get_scene_info prints.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from blender_mcp import blender_scripts

_BLENDER_CANDIDATES = (
    "/Applications/Blender.app/Contents/MacOS/Blender",
)


def _blender_bin():
    found = shutil.which("blender")
    if found:
        return found
    for path in _BLENDER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


# Runs inside Blender. Builds the curve the bug report describes: a small
# beveled poly parented to a mesh, then compares three sizes.
_DRIVER = r"""
import contextlib
import io
import json
import math
import sys

import bpy
from mathutils import Vector

fn_path, summary_path, out_path = sys.argv[sys.argv.index("--") + 1:]

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()

bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0.3, -0.2, 0.15))
parent = bpy.context.active_object
parent.name = "Body"
parent.rotation_euler = (0.2, -0.4, 0.5)
parent.scale = (1.1, 0.9, 1.05)

curve_data = bpy.data.curves.new("trim", "CURVE")
curve_data.dimensions = "3D"
curve_data.bevel_depth = 0.012
curve_data.bevel_resolution = 3
spline = curve_data.splines.new("POLY")
coords = [(0.0, 0.0, 0.0), (0.09, 0.0, 0.0), (0.09, 0.05, 0.005), (0.01, 0.04, 0.03)]
spline.points.add(len(coords) - 1)
for point, co in zip(spline.points, coords):
    point.co = (co[0], co[1], co[2], 1.0)

obj = bpy.data.objects.new("Trim", curve_data)
bpy.context.collection.objects.link(obj)
obj.parent = parent
obj.location = (0.02, -0.01, 0.04)

bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()
ev = obj.evaluated_get(depsgraph)
mesh = ev.to_mesh()
try:
    verts = [ev.matrix_world @ vertex.co for vertex in mesh.vertices]
finally:
    ev.to_mesh_clear()
if len(verts) < 8:
    raise SystemExit("beveled curve produced no evaluated mesh")
true_size = [max(p[i] for p in verts) - min(p[i] for p in verts) for i in range(3)]

namespace = {"math": math, "Vector": Vector}
exec(open(fn_path, encoding="utf-8").read(), namespace)
lo, hi = namespace["world_bounds"](obj, depsgraph)
fn_size = [hi[i] - lo[i] for i in range(3)]

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(open(summary_path, encoding="utf-8").read(), {})
marker = "__MCP_RESULT__"
line = next(item for item in reversed(buf.getvalue().splitlines()) if item.startswith(marker))
summary = json.loads(line[len(marker):])

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"true_size": true_size, "fn_size": fn_size, "lines": summary["lines"]}, handle)
"""


def test_scene_summary_and_look_share_world_bounds():
    fn = blender_scripts.WORLD_BOUNDS_FN
    assert fn in blender_scripts.SCENE_SUMMARY
    assert fn in blender_scripts.LOOK
    assert fn in blender_scripts.BOUNDS
    assert "world_bounds(" in blender_scripts.LOOK


def test_beveled_curve_size_matches_evaluated_mesh(tmp_path):
    blender = _blender_bin()
    if blender is None:
        pytest.skip("Blender is not installed")

    fn_path = tmp_path / "world_bounds.py"
    summary_path = tmp_path / "summary.py"
    out_path = tmp_path / "result.json"
    driver = tmp_path / "driver.py"
    fn_path.write_text(blender_scripts.WORLD_BOUNDS_FN, encoding="utf-8")
    summary_path.write_text(
        blender_scripts.build(
            blender_scripts.SCENE_SUMMARY,
            {"fields": ["size", "parent"], "query": "trim", "limit": 20},
        ),
        encoding="utf-8",
    )
    driver.write_text(_DRIVER, encoding="utf-8")

    proc = subprocess.run(
        [blender, "--background", "--python", str(driver), "--",
         str(fn_path), str(summary_path), str(out_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout[-2000:] + proc.stderr[-2000:])
    result = json.loads(out_path.read_text(encoding="utf-8"))

    true_size = result["true_size"]
    fn_size = result["fn_size"]
    # The broken bound_box is about 2 m. The beveled curve is a few centimetres.
    assert max(true_size) < 0.5, true_size
    for got, truth in zip(fn_size, true_size):
        assert abs(got - truth) <= 0.03 * truth, (fn_size, true_size)

    line = next(item for item in result["lines"] if item.startswith("Trim"))
    reported = [float(part) for part in re.search(r"size ([0-9.]+)x([0-9.]+)x([0-9.]+)", line).groups()]
    # The summary rounds to 2 decimals, so allow half a centimetre on top of a few percent.
    for got, truth in zip(reported, true_size):
        assert abs(got - truth) <= max(0.03 * truth, 0.011), (reported, true_size, line)
