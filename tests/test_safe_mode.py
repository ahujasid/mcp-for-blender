"""Tests for the opt-in safe-mode validator on execute_blender_code.

Two properties matter: representative real-world bpy scripts (including the
render/save/import/export work the desktop-app sandbox forbids) must pass, and
every escape family — interpreter, filesystem, persistence, and the
container-wrap / alias-walk bypasses of the path rules — must fail.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blender_mcp.safe_mode import (  # noqa: E402
    SAFE_MODE_ENV,
    SandboxViolation,
    is_safe,
    safe_mode_enabled,
    validate_code,
)


# --- env toggle -----------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(SAFE_MODE_ENV, raising=False)
    assert not safe_mode_enabled()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_enabled_values(monkeypatch, value):
    monkeypatch.setenv(SAFE_MODE_ENV, value)
    assert safe_mode_enabled()


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "banana"])
def test_disabled_values(monkeypatch, value):
    monkeypatch.setenv(SAFE_MODE_ENV, value)
    assert not safe_mode_enabled()


# --- legitimate scripts must pass -----------------------------------------

ALLOWED_SCRIPTS = {
    "create_cube": (
        "import bpy\n"
        "bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))\n"
        "cube = bpy.context.active_object\n"
        "cube.name = 'MyCube'\n"
        "cube.location.x += 1.5\n"
    ),
    "materials_and_nodes": (
        "import bpy\n"
        "mat = bpy.data.materials.new(name='Red')\n"
        "mat.use_nodes = True\n"
        "bsdf = mat.node_tree.nodes['Principled BSDF']\n"
        "bsdf.inputs['Base Color'].default_value = (1, 0, 0, 1)\n"
        "bpy.context.active_object.data.materials.append(mat)\n"
    ),
    # Blocked in the desktop-app policy, core use cases here.
    "render_to_file": (
        "import bpy\n"
        "scene = bpy.context.scene\n"
        "scene.render.filepath = '/tmp/render.png'\n"
        "scene.render.resolution_x = 1920\n"
        "bpy.ops.render.render(write_still=True)\n"
    ),
    "save_and_export": (
        "import bpy\n"
        "bpy.ops.wm.save_as_mainfile(filepath='/tmp/scene.blend')\n"
        "bpy.ops.wm.obj_export(filepath='/tmp/scene.obj')\n"
        "bpy.ops.export_scene.fbx(filepath='/tmp/scene.fbx')\n"
    ),
    "import_and_load": (
        "import bpy\n"
        "bpy.ops.wm.obj_import(filepath='/tmp/model.obj')\n"
        "img = bpy.data.images.load('/tmp/tex.png')\n"
    ),
    "open_mainfile": (
        "import bpy\n"
        "bpy.ops.wm.open_mainfile(filepath='/tmp/other.blend')\n"
    ),
    "bmesh_and_math": (
        "import bpy\n"
        "import bmesh\n"
        "import math\n"
        "from mathutils import Vector\n"
        "bm = bmesh.new()\n"
        "bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=1.0)\n"
        "for v in bm.verts:\n"
        "    v.co += Vector((0, 0, math.sin(v.co.x)))\n"
        "mesh = bpy.data.meshes.new('Wavy')\n"
        "bm.to_mesh(mesh)\n"
        "bm.free()\n"
    ),
    "functions_loops_fstrings": (
        "import bpy\n"
        "def grid(n):\n"
        "    for i in range(n):\n"
        "        for j in range(n):\n"
        "            bpy.ops.mesh.primitive_cube_add(location=(i * 2, j * 2, 0))\n"
        "            bpy.context.active_object.name = f'cube_{i}_{j}'\n"
        "grid(3)\n"
        "print(f'{len(bpy.data.objects)} objects')\n"
    ),
    "scene_iteration": (
        "import bpy\n"
        "meshes = [o for o in bpy.data.objects if o.type == 'MESH']\n"
        "meshes.sort(key=repr)\n"
        "for obj in meshes:\n"
        "    obj.select_set(True)\n"
        "sub = bpy.data.scenes[0].render.resolution_x\n"
        "print(sub)\n"
    ),
    "collection_link": (
        "import bpy\n"
        "light_data = bpy.data.lights.new(name='Sun', type='SUN')\n"
        "light = bpy.data.objects.new(name='Sun', object_data=light_data)\n"
        "bpy.context.collection.objects.link(light)\n"
    ),
    "try_except": (
        "import bpy\n"
        "try:\n"
        "    obj = bpy.data.objects['Cube']\n"
        "except KeyError as e:\n"
        "    print('missing:', e)\n"
    ),
    # Reading ordinary preferences must keep working: only the script search
    # path and autoexec toggle are blocked, not `filepaths` as a whole.
    "read_preferences": (
        "import bpy\n"
        "prefs = bpy.context.preferences\n"
        "print(prefs.filepaths.temporary_directory)\n"
        "print(prefs.filepaths.render_output_directory)\n"
        "prefs.view.show_splash = False\n"
    ),
}


@pytest.mark.parametrize("name", sorted(ALLOWED_SCRIPTS))
def test_allows_legitimate_script(name):
    validate_code(ALLOWED_SCRIPTS[name])  # must not raise


# --- escapes must fail ----------------------------------------------------

BLOCKED_SCRIPTS = {
    # interpreter escapes
    "eval": "eval('1+1')",
    "exec": "exec('import os')",
    "open_builtin": "data = open('/etc/passwd').read()\nprint(data)",
    "dunder_ladder": "x = ().__class__.__bases__[0].__subclasses__()",
    "computed_getattr": "import bpy\nm = getattr(bpy, 'ap' + 'p')",
    "type_factory": "C = type('C', (), {})",
    # module policy
    "import_os": "import os\nprint(os.listdir('/'))",
    "import_subprocess": "import subprocess\nsubprocess.run(['ls'])",
    "import_socket_mod": "import socket",
    "import_alias": "import bpy as b\nprint(b.data)",
    "from_os": "from os import system",
    "numpy": "import numpy\nnumpy.load('/tmp/x.npy')",
    # persistence
    "handlers": "import bpy\nbpy.app.handlers.frame_change_post.clear()",
    "timers": "import bpy\nbpy.app.timers.register(print)",
    "driver_add": "import bpy\nbpy.context.object.driver_add('location', 0)",
    "driver_expression": (
        "import bpy\n"
        "fc = bpy.context.object.animation_data.drivers[0]\n"
        "fc.driver.expression = '1+1'\n"
    ),
    "register_class": "import bpy\nbpy.utils.register_class(None)",
    "rna_assign": "import bpy\nbpy.types.Scene.evil = None",
    # code-execution operators and datablocks
    "ops_script": "import bpy\nbpy.ops.script.python_file_run(filepath='/tmp/x.py')",
    "ops_text": "import bpy\nbpy.ops.text.run_script()",
    "addon_install": "import bpy\nbpy.ops.preferences.addon_install(filepath='/tmp/x.zip')",
    "extension_install_files": (
        "import bpy\n"
        "bpy.ops.extensions.package_install_files("
        "filepath='/tmp/x.zip', repo='user_default', enable_on_install=True)\n"
    ),
    "extension_install_remote": (
        "import bpy\nbpy.ops.extensions.package_install(repo_index=0, pkg_id='x')"
    ),
    # Script search path (issue #365). `script_directories` is the live 3.6+
    # collection; `script_directory` is the pre-3.6 string property.
    "script_directories_new": (
        "import bpy\n"
        "sd = bpy.context.preferences.filepaths.script_directories.new()\n"
        "sd.directory = '/tmp/evil_scripts'\n"
    ),
    "script_directories_via_alias": (
        "import bpy\n"
        "prefs = bpy.context.preferences\n"
        "prefs.filepaths.script_directories.new().directory = '/tmp/evil_scripts'\n"
    ),
    "script_directories_iterate": (
        "import bpy\n"
        "for sd in bpy.context.preferences.filepaths.script_directories:\n"
        "    sd.directory = '/tmp/evil_scripts'\n"
    ),
    "script_directory_legacy": (
        "import bpy\nbpy.context.preferences.filepaths.script_directory = '/tmp/evil'"
    ),
    "scripts_auto_execute": (
        "import bpy\nbpy.context.preferences.filepaths.use_scripts_auto_execute = True"
    ),
    "texts": "import bpy\nt = bpy.data.texts.new('x')",
    "external_blend": "import bpy\nbpy.ops.wm.append(filepath='/tmp/evil.blend')",
    "libraries": "import bpy\nprint(bpy.data.libraries)",
    "url_open": "import bpy\nbpy.ops.wm.url_open(url='http://evil.example')",
    "save_homefile": "import bpy\nbpy.ops.wm.save_homefile()",
    # bypass shapes
    "container_wrap": "import bpy\n[bpy][0].ops.script.python_file_run(filepath='/tmp/x.py')",
    "alias_walk": "import bpy\nd = bpy.data\nt = d.texts",
    "from_bpy_import": "from bpy import ops\nops.wm.append(filepath='/tmp/evil.blend')",
    "alias_ops_namespace": "import bpy\no = bpy.ops\no.wm.append(filepath='/tmp/evil.blend')",
    "namespace_as_argument": (
        "import bpy\n"
        "def f(m):\n"
        "    m.wm.link(filepath='/tmp/evil.blend')\n"
        "f(bpy.ops)\n"
    ),
    "module_in_container": (
        "import bpy\n"
        "x = [bpy]\n"
        "x[0].ops.wm.append(filepath='/tmp/evil.blend')\n"
    ),
    "getattr_namespace": "import bpy\no = getattr(bpy, 'ops')",
    "shadow_bpy": "import bpy\nbpy = None",
    "shadow_builtin": "print = None",
    "lambda_call": "f = lambda: 1\nf()",
    "unknown_name": "mystery_helper()",
    "class_def": "class Evil:\n    pass",
}


@pytest.mark.parametrize("name", sorted(BLOCKED_SCRIPTS))
def test_blocks_escape(name):
    with pytest.raises(SandboxViolation):
        validate_code(BLOCKED_SCRIPTS[name])


def test_violation_carries_line_number():
    with pytest.raises(SandboxViolation) as exc_info:
        validate_code("import bpy\nx = 1\neval('1')")
    assert exc_info.value.node_line == 3
    assert "line 3" in str(exc_info.value)


def test_syntax_error_is_a_violation():
    with pytest.raises(SandboxViolation, match="syntax error"):
        validate_code("def broken(:\n    pass")


def test_size_limit():
    with pytest.raises(SandboxViolation, match="bytes"):
        validate_code("x = 1\n" * 40_000)


def test_is_safe_returns_reason():
    ok, reason = is_safe("import os")
    assert not ok
    assert "os" in reason
    ok, reason = is_safe("import bpy\nprint(bpy.data.objects)")
    assert ok
    assert reason == ""


# --- server wiring --------------------------------------------------------


def test_execute_blender_code_short_circuits(monkeypatch):
    """With safe mode on, a rejected script never reaches the socket."""
    pytest.importorskip("mcp")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv(SAFE_MODE_ENV, "1")

    from blender_mcp import server

    def explode():
        raise AssertionError("must not connect to Blender for a rejected script")

    monkeypatch.setattr(server, "get_blender_connection", explode)

    result = asyncio.run(server.execute_blender_code(ctx=None, code="import os"))
    assert "Rejected by safe mode" in result
    assert SAFE_MODE_ENV in result


def test_execute_blender_code_passes_valid_code_through(monkeypatch):
    """With safe mode on, a clean script proceeds to send_command."""
    pytest.importorskip("mcp")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv(SAFE_MODE_ENV, "1")

    from blender_mcp import server

    sent = {}

    class FakeConnection:
        def send_command(self, command, params):
            sent["command"] = command
            sent["params"] = params
            return {"result": "ok"}

    monkeypatch.setattr(server, "get_blender_connection", lambda: FakeConnection())

    code = "import bpy\nbpy.ops.mesh.primitive_cube_add()"
    result = asyncio.run(server.execute_blender_code(ctx=None, code=code))
    assert "Code executed successfully" in result
    assert sent == {"command": "execute_code", "params": {"code": code}}


def test_execute_blender_code_skips_validation_when_off(monkeypatch):
    """Safe mode off: even a hostile-looking script goes straight through."""
    pytest.importorskip("mcp")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.delenv(SAFE_MODE_ENV, raising=False)

    from blender_mcp import server

    class FakeConnection:
        def send_command(self, command, params):
            return {"result": "ran"}

    monkeypatch.setattr(server, "get_blender_connection", lambda: FakeConnection())

    result = asyncio.run(server.execute_blender_code(ctx=None, code="import os"))
    assert "Code executed successfully" in result
