"""Regression coverage for Hunyuan import URL routing and zip-slip checks."""
import importlib.util
import io
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


def test_zip_member_is_inside_rejects_prefix_sibling(tmp_path, monkeypatch):
    """A member whose path only shares a string prefix with temp_dir must be rejected.

    ``startswith(temp_dir)`` without a trailing separator would accept
    ``/tmp/tmpXYZ-evil/x`` when extracting into ``/tmp/tmpXYZ``.
    """
    addon, _bpy = _load_addon(monkeypatch)
    temp_dir = tmp_path / "tmpXYZ"
    temp_dir.mkdir()
    assert addon._zip_member_is_inside(str(temp_dir), "model.obj")
    assert addon._zip_member_is_inside(str(temp_dir), "subdir/model.obj")
    assert not addon._zip_member_is_inside(str(temp_dir), "../evil.txt")
    assert not addon._zip_member_is_inside(str(temp_dir), str(tmp_path / "tmpXYZ-evil" / "x"))


def test_hunyuan_zip_imports_uppercase_obj(monkeypatch, tmp_path):
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    called = {"obj": False}

    def obj_import(filepath):
        called["obj"] = True
        assert filepath.lower().endswith(".obj")
        mesh = types.SimpleNamespace(
            type="MESH",
            name="Imported",
            location=types.SimpleNamespace(x=0, y=0, z=0),
            rotation_euler=types.SimpleNamespace(x=0, y=0, z=0),
            scale=types.SimpleNamespace(x=1, y=1, z=1),
        )
        bpy.context.selected_objects = [mesh]

    bpy.ops.wm.obj_import = obj_import
    monkeypatch.setattr(server, "_get_aabb", lambda _obj: [0, 0, 0, 1, 1, 1])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MODEL.OBJ", "o test\n")
    payload = buf.getvalue()
    monkeypatch.setattr(
        addon.requests,
        "get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
        raising=False,
    )

    result = server.import_generated_asset_hunyuan_ai(
        "UpperObj",
        "https://example.com/models/asset.zip",
    )
    assert called["obj"] is True
    assert result["succeed"] is True
    assert result["name"] == "UpperObj"
