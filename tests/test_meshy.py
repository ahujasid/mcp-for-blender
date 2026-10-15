"""Meshy generation stays behind the sidebar switch and never echoes the API key."""
import base64
import importlib.util
import sys
import types

import pytest

from conftest import ROOT_ADDON as ADDON

KEY = "msy-super-secret-meshy"


def _install_bpy_stubs(monkeypatch, scene):
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        preferences=types.SimpleNamespace(addons={}),
        view_layer=types.SimpleNamespace(update=lambda: None),
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace(
        import_scene=types.SimpleNamespace(gltf=lambda **_kwargs: None),
    )
    bpy.data = types.SimpleNamespace(objects=[])

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

    def no_network(*_a, **_k):
        raise AssertionError("tests must not reach the network")

    requests.get = requests.post = no_network
    monkeypatch.setitem(sys.modules, "requests", requests)
    return bpy


def _load_addon(monkeypatch, **scene_attrs):
    monkeypatch.delenv("BLENDERMCP_MESHY_API_KEY", raising=False)
    monkeypatch.delenv("BLENDERMCP_PREMIUM_LICENSE_KEY", raising=False)
    scene = types.SimpleNamespace(
        blendermcp_use_meshy=False,
        blendermcp_meshy_model="latest",
        blendermcp_meshy_api_key="",
        blendermcp_use_tripo=False,
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_hunyuan3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
    )
    for key, value in scene_attrs.items():
        setattr(scene, key, value)
    bpy = _install_bpy_stubs(monkeypatch, scene)
    spec = importlib.util.spec_from_file_location("blender_mcp_addon_meshy_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon, bpy, scene


def _ready(monkeypatch, **scene_attrs):
    attrs = {"blendermcp_use_meshy": True, "blendermcp_meshy_api_key": KEY, **scene_attrs}
    return _load_addon(monkeypatch, **attrs)


class _JsonResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


def _capture_posts(addon, reply=None, status_code=200):
    posts = []

    def post(url, headers=None, json=None, **kwargs):
        posts.append({"url": url, "headers": headers or {}, "json": json, "kwargs": kwargs})
        return _JsonResponse(reply if reply is not None else {"result": f"task_{len(posts)}"}, status_code)

    addon.requests.post = post
    return posts


# ------------------------------------------------------------------ status

def test_meshy_status_reports_sidebar_state(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    disabled = server.get_meshy_status()
    assert disabled["enabled"] is False
    assert "AI Model Generation" in disabled["message"]
    assert "Meshy" in disabled["message"]

    scene.blendermcp_use_meshy = True
    missing_key = server.get_meshy_status()
    assert missing_key["enabled"] is False
    assert "API key" in missing_key["message"]

    scene.blendermcp_meshy_api_key = KEY
    scene.blendermcp_meshy_model = "meshy-6"
    ready = server.get_meshy_status()
    assert ready["enabled"] is True
    assert ready["model"] == "meshy-6"
    assert "Meshy 7.1" in ready["message"]
    assert KEY not in str(ready)


def test_meshy_is_off_under_premium(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    monkeypatch.setattr(addon, "premium_active", lambda: True)
    server = addon.BlenderMCPServer()

    status = server.get_meshy_status()
    assert status["enabled"] is False
    assert "Premium" in status["message"]

    blocked = server._execute_command_internal({"type": "create_meshy_job", "params": {"text_prompt": "a teapot"}})
    assert blocked["status"] == "error"


# ------------------------------------------------------------------ create

def test_meshy_text_job_starts_a_preview_with_real_world_scale(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch, blendermcp_meshy_model="meshy-6")
    posts = _capture_posts(addon, {"result": "prev_1"})
    server = addon.BlenderMCPServer()

    result = server.create_meshy_job(text_prompt="a ceramic teapot")

    assert result == {"task_id": "prev_1", "kind": "t2p", "model": "meshy-6"}
    assert len(posts) == 1
    assert posts[0]["url"] == "https://api.meshy.ai/openapi/v2/text-to-3d"
    assert posts[0]["json"] == {
        "mode": "preview", "prompt": "a ceramic teapot", "ai_model": "meshy-6",
        "target_formats": ["glb"], "auto_size": True, "origin_at": "bottom",
    }
    assert posts[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert posts[0]["headers"]["User-Agent"].startswith("mcp-for-blender/")
    assert KEY not in str(result)


def test_meshy_image_url_is_one_textured_task(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, {"result": "img_1"})
    server = addon.BlenderMCPServer()

    result = server.create_meshy_job(image="https://example.com/teapot.png")

    assert result == {"task_id": "img_1", "kind": "i2m", "model": "latest"}
    assert posts[0]["url"] == "https://api.meshy.ai/openapi/v1/image-to-3d"
    assert posts[0]["json"] == {
        "image_url": "https://example.com/teapot.png", "ai_model": "latest", "should_texture": True,
        "enable_pbr": True, "target_formats": ["glb"], "auto_size": True, "origin_at": "bottom",
    }


def test_meshy_local_image_becomes_a_data_uri(monkeypatch, tmp_path):
    image = tmp_path / "ref.webp"
    image.write_bytes(b"RIFF-webp")
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, {"result": "img_2"})
    server = addon.BlenderMCPServer()

    result = server.create_meshy_job(image=str(image))

    assert result["task_id"] == "img_2"
    assert len(posts) == 1
    expected = "data:image/webp;base64," + base64.b64encode(b"RIFF-webp").decode("ascii")
    assert posts[0]["json"]["image_url"] == expected


@pytest.mark.parametrize("kwargs, needle", [
    ({"text_prompt": "a teapot", "image": "https://example.com/a.png"}, "not both"),
    ({}, "not both"),
    ({"text_prompt": "x" * 801}, "800"),
    ({"image": "/no/such/file.png"}, "not found"),
])
def test_meshy_rejects_bad_input_without_calling_api(monkeypatch, tmp_path, kwargs, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    server = addon.BlenderMCPServer()

    result = server.create_meshy_job(**kwargs)

    assert needle in result["error"]
    assert posts == []


def test_meshy_rejects_unsupported_image_type(monkeypatch, tmp_path):
    image = tmp_path / "ref.gif"
    image.write_bytes(b"GIF89a")
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_meshy_job(image=str(image))

    assert "error" in result and posts == []


@pytest.mark.parametrize("status_code, message", [
    (401, "Meshy rejected the API key"),
    (402, "Out of Meshy credits. Top up at meshy.ai"),
    (429, "Meshy rate limit hit, wait and retry"),
    (400, "Prompt is invalid"),
])
def test_meshy_errors_are_readable_and_never_show_the_key(monkeypatch, status_code, message):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, {"message": "Prompt is invalid"}, status_code)
    server = addon.BlenderMCPServer()

    result = server.create_meshy_job(text_prompt="a teapot")

    assert result["error"] == message
    assert KEY not in str(result)
    assert len(posts) == 1


def test_meshy_network_failure_never_shows_the_key(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)

    def post(*_a, **_k):
        raise ConnectionError(f"boom Authorization: Bearer {KEY}")

    addon.requests.post = post
    result = addon.BlenderMCPServer().create_meshy_job(text_prompt="a teapot")

    assert "error" in result
    assert KEY not in str(result)


# ------------------------------------------------------------------ refine

def test_meshy_refine_inherits_the_preview_and_happens_once(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, {"result": "ref_1"})
    server = addon.BlenderMCPServer()

    first = server.refine_meshy_job(preview_task_id="prev_1")
    # A socket timeout on the MCP side means the server never saw `first`; asking
    # again must return the same refine, not pay for another.
    second = server.refine_meshy_job(preview_task_id="prev_1")

    assert first == second == {"task_id": "ref_1", "kind": "t2r"}
    assert len(posts) == 1
    assert posts[0]["url"] == "https://api.meshy.ai/openapi/v2/text-to-3d"
    assert posts[0]["json"] == {
        "mode": "refine", "preview_task_id": "prev_1", "enable_pbr": True,
        "target_formats": ["glb"], "auto_size": True, "origin_at": "bottom",
    }


def test_meshy_failed_refine_is_not_remembered(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    _capture_posts(addon, {"message": "nope"}, 429)
    server = addon.BlenderMCPServer()
    assert "rate limit" in server.refine_meshy_job(preview_task_id="prev_9")["error"]

    posts = _capture_posts(addon, {"result": "ref_9"})
    assert server.refine_meshy_job(preview_task_id="prev_9")["task_id"] == "ref_9"
    assert len(posts) == 1


# ------------------------------------------------------------------ poll

@pytest.mark.parametrize("kind, endpoint", [
    ("t2p", "/v2/text-to-3d/"),
    ("t2r", "/v2/text-to-3d/"),
    ("i2m", "/v1/image-to-3d/"),
])
def test_meshy_poll_picks_endpoint_and_returns_glb(monkeypatch, kind, endpoint):
    addon, _bpy, _scene = _ready(monkeypatch)
    seen = {}

    def get(url, headers=None, **_kwargs):
        seen["url"] = url
        return _JsonResponse({"id": "t/1", "status": "SUCCEEDED", "progress": 100,
                              "model_urls": {"glb": "https://assets.meshy.ai/t1.glb"}})

    addon.requests.get = get
    result = addon.BlenderMCPServer().poll_meshy_job_status(task_id="t/1", kind=kind)

    assert result == {"status": "SUCCEEDED", "progress": 100, "model_url": "https://assets.meshy.ai/t1.glb"}
    assert endpoint in seen["url"]
    assert seen["url"].endswith("t%2F1")


def test_meshy_poll_relays_task_error(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    addon.requests.get = lambda url, headers=None, **_k: _JsonResponse(
        {"status": "FAILED", "progress": 40, "task_error": {"message": "Prompt flagged"}})

    result = addon.BlenderMCPServer().poll_meshy_job_status(task_id="t1", kind="t2p")

    assert result["status"] == "FAILED"
    assert result["error"] == "Prompt flagged"


def test_meshy_poll_rejects_unknown_kind(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    assert "error" in addon.BlenderMCPServer().poll_meshy_job_status(task_id="t1", kind="nope")


# ------------------------------------------------------------------ import

def test_meshy_import_rejects_non_http_url(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    result = addon.BlenderMCPServer().import_generated_asset_meshy(name="Teapot", model_url="/tmp/model.glb")
    assert result["succeed"] is False


def test_meshy_import_downloads_glb(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    downloaded = {}

    class _Stream:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield b"glb-bytes"

    def get(url, headers=None, stream=False, **_k):
        downloaded["user_agent"] = (headers or {}).get("User-Agent", "")
        assert "Authorization" not in (headers or {})
        return _Stream()

    addon.requests.get = get

    def fake_clean(filepath, mesh_name=None):
        with open(filepath, "rb") as handle:
            downloaded["bytes"] = handle.read()
        return types.SimpleNamespace(
            name=mesh_name,
            type="MESH",
            location=types.SimpleNamespace(x=1, y=2, z=3),
            rotation_euler=types.SimpleNamespace(x=0, y=0, z=0),
            scale=types.SimpleNamespace(x=1, y=1, z=1),
        )

    server._clean_imported_glb = fake_clean
    server._get_aabb = lambda _obj: [[0, 0, 0], [1, 1, 1]]

    result = server.import_generated_asset_meshy(name="Teapot", model_url="https://assets.meshy.ai/t1.glb")

    assert result["succeed"] is True
    assert result["name"] == "Teapot"
    assert result["world_bounding_box"] == [[0, 0, 0], [1, 1, 1]]
    assert downloaded["bytes"] == b"glb-bytes"
    assert downloaded["user_agent"].startswith("mcp-for-blender/")


# ------------------------------------------------------------------ dispatch

def test_meshy_commands_stay_unregistered_until_enabled(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch, blendermcp_meshy_api_key=KEY)
    server = addon.BlenderMCPServer()

    for command in ("create_meshy_job", "refine_meshy_job", "poll_meshy_job_status", "import_generated_asset_meshy"):
        blocked = server._execute_command_internal({"type": command, "params": {}})
        assert blocked["status"] == "error"
        assert "Unknown command type" in blocked["message"]

    scene.blendermcp_use_meshy = True
    posts = _capture_posts(addon, {"result": "prev_1"})
    allowed = server._execute_command_internal({"type": "create_meshy_job", "params": {"text_prompt": "a teapot"}})
    assert allowed["status"] == "success"
    assert allowed["result"]["task_id"] == "prev_1"
    assert posts


def test_meshy_status_is_always_answered(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    reply = addon.BlenderMCPServer()._execute_command_internal({"type": "get_meshy_status", "params": {}})
    assert reply["status"] == "success"
    assert reply["result"]["enabled"] is False
