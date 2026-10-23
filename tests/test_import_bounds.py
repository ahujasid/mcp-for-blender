"""Import bounds measure the mesh, not the rig it is parented to.

An armature's bound_box can be larger than the mesh it deforms. Folding that
box into BOUNDS makes a rigged import look bigger than it is.
"""

import json
import os
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


# Runs inside Blender. A small mesh parented to a much larger armature, then
# the import-bounds script measured on the armature (the imported root).
_DRIVER = r"""
import contextlib
import io
import json
import sys

import bpy
from mathutils import Vector

bounds_path, out_path = sys.argv[sys.argv.index("--") + 1:]

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()

bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0.0, 0.0, 0.2))
mesh = bpy.context.active_object
mesh.name = "Body"

bpy.ops.object.armature_add(location=(0.0, 0.0, 0.0))
arm = bpy.context.active_object
arm.name = "Rig"
bpy.ops.object.mode_set(mode="EDIT")
bone = arm.data.edit_bones[0]
bone.head = (-1.5, -1.5, 0.0)
bone.tail = (1.5, 1.5, 3.0)
bpy.ops.object.mode_set(mode="OBJECT")

mesh.parent = arm
mesh.matrix_parent_inverse = arm.matrix_world.inverted()
bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()

def world_size(obj):
    ev = obj.evaluated_get(depsgraph)
    pts = [ev.matrix_world @ Vector(corner) for corner in ev.bound_box]
    return [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(open(bounds_path, encoding="utf-8").read(), {})
marker = "__MCP_RESULT__"
line = next(item for item in reversed(buf.getvalue().splitlines()) if item.startswith(marker))
reported = json.loads(line[len(marker):])

with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({
        "mesh_size": world_size(mesh),
        "arm_size": world_size(arm),
        "bounds": reported,
    }, handle)
"""


def test_mesh_parented_to_armature_reports_mesh_size(tmp_path):
    blender = _blender_bin()
    if blender is None:
        pytest.skip("Blender is not installed")

    bounds_path = tmp_path / "bounds.py"
    out_path = tmp_path / "result.json"
    driver = tmp_path / "driver.py"
    bounds_path.write_text(
        blender_scripts.build(blender_scripts.BOUNDS, {"names": ["Rig"]}),
        encoding="utf-8",
    )
    driver.write_text(_DRIVER, encoding="utf-8")

    proc = subprocess.run(
        [blender, "--background", "--python", str(driver), "--",
         str(bounds_path), str(out_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout[-2000:] + proc.stderr[-2000:])
    result = json.loads(out_path.read_text(encoding="utf-8"))

    mesh_size = result["mesh_size"]
    arm_size = result["arm_size"]
    # The rig is the thing that used to inflate the report. If it is not
    # actually larger, this scene no longer exercises the filter.
    assert max(arm_size) > max(mesh_size) + 0.5, (arm_size, mesh_size)
    assert result["bounds"] and result["bounds"][0]["name"] == "Rig"
    reported = result["bounds"][0]["size"]
    # BOUNDS rounds each corner to 3 decimals before subtracting.
    for got, truth in zip(reported, mesh_size):
        assert abs(got - truth) <= 0.002, (reported, mesh_size, arm_size)
