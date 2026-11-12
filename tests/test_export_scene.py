"""export_scene: write the whole scene, the current selection, or named objects to GLB/FBX."""
import asyncio
import importlib.util
import json
import os
import sys
import types

from conftest import ROOT_ADDON as ADDON


class _Object:
    def __init__(self, name, children=()):
        self.name = name
        self.children_recursive = list(children)
        self.selected = False

    def select_set(self, value):
        self.selected = value


class _Objects:
    def __init__(self, *objects):
        self._all = list(objects)
        self._by_name = {o.name: o for o in objects}

    def get(self, name):
        return self._by_name.get(name)

    def __getitem__(self, name):
        return self._by_name[name]

    def __iter__(self):
        return iter(self._all)


def _load_addon(monkeypatch, objects, selected, exports):
    scene = types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=False,
        objects=objects,
    )
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        object=None,
        selected_objects=selected,
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
    )
    bpy.data = types.SimpleNamespace(objects=objects)
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    def export(kind):
        def _op(filepath, **kwargs):
            exports.append({"kind": kind, "filepath": filepath, **kwargs})
            with open(filepath, "wb") as fh:
                fh.write(b"EXPORTED")
        return _op

    bpy.ops = types.SimpleNamespace(
        object=types.SimpleNamespace(select_all=lambda action: None, mode_set=lambda mode: None),
        export_scene=types.SimpleNamespace(gltf=export("gltf"), fbx=export("fbx")),
    )

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 2, 0)
    app.version_string = "4.2.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def test_named_objects_export_with_their_children_as_glb(monkeypatch, tmp_path):
    leg = _Object("Leg")
    chair = _Object("Chair", children=[leg])
    lamp = _Object("Lamp")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(chair, leg, lamp), [], exports)
    out = str(tmp_path / "chair.glb")

    result = addon.BlenderMCPServer().export_scene(out, object_names=["Chair"])

    assert result == {"path": out, "bytes": len(b"EXPORTED"), "selection_only": True, "exported": ["Chair", "Leg"]}
    assert chair.selected and leg.selected and not lamp.selected
    assert exports[0]["kind"] == "gltf"
    assert exports[0]["use_selection"] is True
    assert exports[0]["use_active_scene"] is True
    assert exports[0]["export_apply"] is True
    assert exports[0]["export_format"] == "GLB"


def test_selection_only_exports_the_current_selection_as_fbx(monkeypatch, tmp_path):
    cube = _Object("Cube")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(cube, _Object("Other")), [cube], exports)
    out = str(tmp_path / "sel.fbx")

    result = addon.BlenderMCPServer().export_scene(out, format="fbx", selection_only=True, apply_modifiers=False)

    assert result["exported"] == ["Cube"]
    assert result["selection_only"] is True
    assert exports[0]["kind"] == "fbx"
    assert exports[0]["use_selection"] is True
    assert exports[0]["use_mesh_modifiers"] is False
    assert exports[0]["bake_space_transform"] is False


def test_whole_scene_when_nothing_is_named_or_selected(monkeypatch, tmp_path):
    objs = _Objects(_Object("A"), _Object("B"))
    exports = []
    addon = _load_addon(monkeypatch, objs, [], exports)
    out = str(tmp_path / "nested" / "scene.glb")

    result = addon.BlenderMCPServer().export_scene(out)

    assert result["selection_only"] is False
    assert result["exported"] == ["A", "B"]
    assert exports[0]["use_selection"] is False
    assert os.path.isdir(str(tmp_path / "nested")), "parent folders are created"


def test_missing_objects_and_bad_input_are_rejected_before_exporting(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)
    server = addon.BlenderMCPServer()
    out = str(tmp_path / "x.glb")

    assert "not found" in server.export_scene(out, object_names=["A", "Ghost"])["error"]
    assert "glb" in server.export_scene(out, format="obj")["error"]
    assert "filepath" in server.export_scene("")["error"]
    assert "Nothing is selected" in server.export_scene(out, selection_only=True)["error"]
    assert exports == []


def test_command_is_always_routed(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)
    out = str(tmp_path / "scene.glb")

    command = addon.BlenderMCPServer()._execute_command_internal({"type": "export_scene", "params": {"filepath": out}})

    assert command["status"] == "success"
    assert command["result"]["exported"] == ["A"]
