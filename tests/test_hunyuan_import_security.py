"""Regression coverage for Hunyuan import URL routing, SSRF, and zip-slip checks."""
import importlib.util
import io
import socket
import sys
import types
import zipfile

from conftest import ROOT_ADDON as ADDON


def _install_bpy_stubs(monkeypatch, scene):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(scene=scene, selected_objects=[])
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(
            gltf=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected gltf import")),
            obj=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected obj import")),
        ),
        wm=types.SimpleNamespace(
            obj_import=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected obj import")),
        ),
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
    return bpy


def _load_addon(monkeypatch):
    scene = types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_hunyuan3d=True,
        blendermcp_use_sketchfab=False,
    )
    bpy = _install_bpy_stubs(monkeypatch, scene)
    spec = importlib.util.spec_from_file_location("blender_mcp_addon_hunyuan_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host in ("127.0.0.1", "localhost") else "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34", port or 0),
            )
        ],
    )
    return addon, bpy


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield self.content


def test_hunyuan_import_prefers_glb_urls(monkeypatch):
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    called = {"gltf": False}

    def gltf_import(**_kwargs):
        called["gltf"] = True
        mesh = types.SimpleNamespace(
            type="MESH",
            name="Imported",
            location=types.SimpleNamespace(x=0, y=0, z=0),
            rotation_euler=types.SimpleNamespace(x=0, y=0, z=0),
            scale=types.SimpleNamespace(x=1, y=1, z=1),
        )
        bpy.context.selected_objects = [mesh]

    bpy.ops.import_scene.gltf = gltf_import
    monkeypatch.setattr(
        addon.requests,
        "get",
        lambda *_args, **_kwargs: _FakeResponse(b"glb-bytes"),
        raising=False,
    )
    monkeypatch.setattr(server, "_get_aabb", lambda _obj: [0, 0, 0, 1, 1, 1])

    result = server.import_generated_asset_hunyuan_ai(
        "Chair",
        "https://example.com/models/asset.GLB?sign=abc",
    )

    assert called["gltf"] is True
    assert result["succeed"] is True
    assert result["name"] == "Chair"


def test_hunyuan_zip_rejects_path_traversal(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "nope")
        zf.writestr("model.obj", "o test\n")
    payload = buf.getvalue()

    monkeypatch.setattr(
        addon.requests,
        "get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
        raising=False,
    )

    result = server.import_generated_asset_hunyuan_ai(
        "BadZip",
        "https://example.com/models/asset.zip",
    )

    assert result["succeed"] is False
    assert "path traversal" in result["error"].lower() or "directory traversal" in result["error"].lower()


def test_hunyuan_import_rejects_local_or_private_urls(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    fetched = []
    monkeypatch.setattr(
        addon.requests,
        "get",
        lambda url, **_kwargs: fetched.append(url) or _FakeResponse(b"nope"),
        raising=False,
    )

    for bad_url in (
        "http://127.0.0.1:8080/internal.glb",
        "http://169.254.169.254/latest/meta-data/model.zip",
    ):
        result = server.import_generated_asset_hunyuan_ai("Asset", bad_url)
        assert result.get("succeed") is False
        assert "local or private" in result["error"]

    assert fetched == []

