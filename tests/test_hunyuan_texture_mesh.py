"""Texturing an existing mesh through a local Hunyuan3D-2 server (LOCAL_API mode).

The addon exports the named mesh to GLB, posts it base64-encoded with texture=True to the
local /generate endpoint, and defers the import of the textured result to a Blender timer.
"""
import asyncio
import base64
import importlib.util
import os
import sys
import types

from conftest import ROOT_ADDON as ADDON


class _Object:
    def __init__(self, name, obj_type="MESH"):
        self.name = name
        self.type = obj_type
        self.parent = None
        self.location = (1.0, 2.0, 3.0)
        self.rotation_euler = (0.0, 0.5, 0.0)
        self.scale = (2.0, 2.0, 2.0)
        self.selected = False

    def select_set(self, value):
        self.selected = value


class _Objects:
    def __init__(self, *objects):
        self._by_name = {o.name: o for o in objects}

    def get(self, name):
        return self._by_name.get(name)

    def __iter__(self):
        return iter(list(self._by_name.values()))


def _load_addon(monkeypatch, scene, objects, exported, timers):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
    )
    bpy.data = types.SimpleNamespace(objects=objects)
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    def export_gltf(filepath, use_selection=False, **_kwargs):
        exported.append({"filepath": filepath, "use_selection": use_selection})
        with open(filepath, "wb") as fh:
            fh.write(b"GLBDATA")

    bpy.ops = types.SimpleNamespace(
        object=types.SimpleNamespace(select_all=lambda action: None),
        export_scene=types.SimpleNamespace(gltf=export_gltf),
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
        register=lambda fn, *_a, **_k: timers.append(fn),
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


def _scene(hunyuan_enabled=True):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=hunyuan_enabled,
        blendermcp_hunyuan3d_mode="LOCAL_API",
        blendermcp_hunyuan3d_octree_resolution=256,
        blendermcp_hunyuan3d_num_inference_steps=20,
        blendermcp_hunyuan3d_guidance_scale=5.5,
    )


class _Response:
    def __init__(self, status_code=200, content=b"TEXTURED-GLB", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def _capture_post(monkeypatch, addon, response=None):
    calls = []

    def fake_post(url, json=None, **_kwargs):
        calls.append({"url": url, "json": json})
        return response or _Response()

    monkeypatch.setattr(addon.requests, "post", fake_post, raising=False)
    return calls


def _server(monkeypatch, addon):
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "_get_hunyuan3d_api_url", lambda: "http://localhost:8081/")
    return server


def test_texturing_posts_the_exported_mesh_to_the_local_server(monkeypatch):
    exported, timers = [], []
    cube = _Object("Cube")
    addon = _load_addon(monkeypatch, _scene(), _Objects(cube), exported, timers)
    server = _server(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    result = server.texture_hunyuan_job_local("Cube", text_prompt="weathered oak")

    assert result["status"] == "DONE"
    assert exported[0]["use_selection"] is True
    assert cube.selected is True
    assert not os.path.exists(exported[0]["filepath"]), "temporary export must be cleaned up"

    call = calls[0]
    assert call["url"] == "http://localhost:8081/generate"
    payload = call["json"]
    assert payload["mesh"] == base64.b64encode(b"GLBDATA").decode()
    assert payload["texture"] is True
    assert payload["text"] == "weathered oak"
    assert payload["octree_resolution"] == 256
    assert payload["num_inference_steps"] == 20
    assert payload["guidance_scale"] == 5.5
    assert "image" not in payload
    assert len(timers) == 1, "the textured result is imported on a Blender timer"


def test_reference_image_file_is_base64_encoded(monkeypatch, tmp_path):
    image = tmp_path / "ref.png"
    image.write_bytes(b"PNGDATA")
    addon = _load_addon(monkeypatch, _scene(), _Objects(_Object("Cube")), [], [])
    server = _server(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    server.texture_hunyuan_job_local("Cube", image=str(image))

    assert calls[0]["json"]["image"] == base64.b64encode(b"PNGDATA").decode("ascii")


def test_missing_or_non_mesh_object_is_rejected_before_any_request(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(), _Objects(_Object("Lamp", obj_type="LIGHT")), [], [])
    server = _server(monkeypatch, addon)
    calls = _capture_post(monkeypatch, addon)

    assert "not found" in server.texture_hunyuan_job_local("Nope")["error"]
    assert "not found" in server.texture_hunyuan_job_local("Lamp")["error"]
    assert calls == []


def test_server_error_is_reported_without_importing(monkeypatch):
    timers = []
    addon = _load_addon(monkeypatch, _scene(), _Objects(_Object("Cube")), [], timers)
    server = _server(monkeypatch, addon)
    _capture_post(monkeypatch, addon, response=_Response(status_code=500, text="boom"))

    result = server.texture_hunyuan_job_local("Cube", text_prompt="x")

    assert "boom" in result["error"]
    assert timers == []


def test_command_is_only_routed_when_hunyuan_is_enabled(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(hunyuan_enabled=False), _Objects(), [], [])
    server = addon.BlenderMCPServer()

    command = server._execute_command_internal({"type": "texture_hunyuan_mesh", "params": {"object_name": "Cube"}})

    assert command == {"status": "error", "message": "Unknown command type: texture_hunyuan_mesh"}


def test_mcp_tool_forwards_the_command_to_blender():
    from blender_mcp import server

    sent = []

    class FakeBlender:
        def send_command(self, command, params=None):
            sent.append((command, params))
            return {"status": "DONE", "message": "ok"}

    original = server.get_blender_connection
    server.get_blender_connection = lambda: FakeBlender()
    try:
        out = asyncio.run(server.texture_mesh_hunyuan3d(
            None, object_name="Cube", text_prompt="oak", input_image_url=None, user_prompt=""))
    finally:
        server.get_blender_connection = original

    assert sent == [("texture_hunyuan_mesh", {"object_name": "Cube", "text_prompt": "oak", "image": None})]
    assert "DONE" in out
