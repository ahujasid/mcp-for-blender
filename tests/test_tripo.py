"""Tripo generation stays behind the sidebar switch and never echoes the API key."""
import importlib.util
import sys
import types

import pytest

from conftest import ROOT_ADDON as ADDON

KEY = "tsk-super-secret-tripo"
MODEL = "v3.1-20260211"


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
    monkeypatch.delenv("BLENDERMCP_TRIPO_API_KEY", raising=False)
    monkeypatch.delenv("BLENDERMCP_PREMIUM_LICENSE_KEY", raising=False)
    scene = types.SimpleNamespace(
        blendermcp_use_tripo=False,
        blendermcp_tripo_model=MODEL,
        blendermcp_tripo_api_key="",
        blendermcp_use_meshy=False,
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_hunyuan3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
    )
    for key, value in scene_attrs.items():
        setattr(scene, key, value)
    bpy = _install_bpy_stubs(monkeypatch, scene)
    spec = importlib.util.spec_from_file_location("blender_mcp_addon_tripo_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon, bpy, scene


def _ready(monkeypatch, **scene_attrs):
    attrs = {"blendermcp_use_tripo": True, "blendermcp_tripo_api_key": KEY, **scene_attrs}
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

    def post(url, headers=None, json=None, files=None, **kwargs):
        posts.append({"url": url, "headers": headers or {}, "json": json, "files": files, "kwargs": kwargs})
        if url.endswith("/files"):
            return _JsonResponse({"code": 0, "data": {"file_token": "file_abc"}}, status_code)
        body = reply if reply is not None else {"code": 0, "data": {"task_id": "task_abc"}}
        return _JsonResponse(body, status_code)

    addon.requests.post = post
    return posts


def test_tripo_status_reports_sidebar_state(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    disabled = server.get_tripo_status()
    assert disabled["enabled"] is False
    assert "AI Model Generation" in disabled["message"]
    assert "Tripo" in disabled["message"]

    scene.blendermcp_use_tripo = True
    missing_key = server.get_tripo_status()
    assert missing_key["enabled"] is False
    assert "API key" in missing_key["message"]
    assert "API Key" in missing_key["message"]

    scene.blendermcp_tripo_api_key = KEY
    scene.blendermcp_tripo_model = "P2-20260801"
    ready = server.get_tripo_status()
    assert ready["enabled"] is True
    assert ready["model"] == "P2-20260801"
    assert "H3.1" in ready["message"]
    assert "P2.0" in ready["message"]
    assert KEY not in str(ready)


def _premium(monkeypatch, addon, jobs=None):
    calls = []

    def premium_request(method, path, payload=None, auth=True):
        calls.append((method, path, payload))
        if method == "POST" and path == "/jobs":
            return {"job_id": "pjob_1"}
        if path.startswith("/jobs/"):
            return (jobs or {}).get(path.rsplit("/", 1)[1], {"code": "NOT_FOUND", "message": "No such job"})
        return {}

    monkeypatch.setattr(addon, "premium_active", lambda: True)
    monkeypatch.setattr(addon, "premium_request", premium_request)
    return calls


def test_tripo_runs_through_premium_without_a_tripo_key(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_tripo=True)
    calls = _premium(monkeypatch, addon)
    server = addon.BlenderMCPServer()

    status = server.get_tripo_status()
    assert status["enabled"] is True
    assert status["mode"] == "PREMIUM"
    assert KEY not in str(status)

    reply = server._execute_command_internal({
        "type": "create_tripo_job",
        "params": {"text_prompt": "a teapot", "quality": "high"},
    })
    assert reply["status"] == "success"
    assert reply["result"]["request_id"] == "pjob_1"
    assert calls[-1][:2] == ("POST", "/jobs")
    assert calls[-1][2]["model"] == "tripo"
    assert calls[-1][2]["prompt"] == "a teapot"
    assert calls[-1][2]["quality"] == "high"
    assert "task_id" not in reply["result"]


def test_tripo_text_job_uses_sidebar_model(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch, blendermcp_tripo_model="P2-20260801")
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(text_prompt="a ceramic teapot")

    assert result["task_id"] == "task_abc"
    assert result["model"] == "P2-20260801"
    assert posts[0]["url"].endswith("/generation/text-to-model")
    assert posts[0]["json"]["model"] == "P2-20260801"
    assert posts[0]["json"]["prompt"] == "a ceramic teapot"
    assert posts[0]["json"]["texture"] is True
    assert posts[0]["json"]["pbr"] is True
    assert posts[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert "quality" not in posts[0]["json"]
    assert KEY not in str(result)


def test_tripo_own_key_drops_quality(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(text_prompt="a teapot", quality="high")

    assert result["task_id"] == "task_abc"
    assert "quality" not in posts[0]["json"]


def test_tripo_image_url_skips_upload(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(image="https://example.com/teapot.png")

    assert result["task_id"] == "task_abc"
    assert len(posts) == 1
    assert posts[0]["url"].endswith("/generation/image-to-model")
    assert posts[0]["json"]["input"] == "https://example.com/teapot.png"
    assert posts[0]["json"]["model"] == MODEL


@pytest.mark.parametrize("name, payload, mime", [
    ("ref.jpg", b"\xff\xd8\xff", "image/jpeg"),
    ("ref.jpeg", b"\xff\xd8\xff", "image/jpeg"),
    ("ref.png", b"\x89PNG\r\n", "image/png"),
])
def test_tripo_local_image_uploads_then_submits(monkeypatch, tmp_path, name, payload, mime):
    image = tmp_path / name
    image.write_bytes(payload)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(image=str(image))

    assert result["task_id"] == "task_abc"
    assert posts[0]["url"].endswith("/files")
    assert posts[0]["files"]["file"][2] == mime
    assert "Authorization" not in str(posts[0]["files"])
    assert posts[1]["url"].endswith("/generation/image-to-model")
    assert posts[1]["json"]["input"] == "file_abc"


@pytest.mark.parametrize("kwargs, needle", [
    ({"text_prompt": "a teapot", "image": "https://example.com/a.png"}, "not both"),
    ({}, "not both"),
    ({"text_prompt": "x" * 1025}, "1024"),
    ({"image": "missing.png"}, "not found"),
])
def test_tripo_rejects_bad_input_without_calling_api(monkeypatch, kwargs, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(**kwargs)
    assert needle in result["error"]
    assert posts == []


def test_tripo_rejects_images_over_20_mb(monkeypatch, tmp_path):
    image = tmp_path / "big.png"
    image.write_bytes(b"\x89PNG")
    addon, _bpy, _scene = _ready(monkeypatch)
    monkeypatch.setattr(addon.os.path, "getsize", lambda _path: addon.TRIPO_IMAGE_MAX_BYTES + 1)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(image=str(image))
    assert result["error"] == "Tripo accepts images up to 20 MB"
    assert posts == []


@pytest.mark.parametrize("name, payload", [
    ("ref.gif", b"GIF89a"),
    ("ref.webp", b"RIFF-webp"),
])
def test_tripo_rejects_unsupported_image_type(monkeypatch, tmp_path, name, payload):
    image = tmp_path / name
    image.write_bytes(payload)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    result = addon.BlenderMCPServer().create_tripo_job(image=str(image))
    assert result["error"] == "Tripo accepts JPEG or PNG images"
    assert posts == []


@pytest.mark.parametrize("status_code, reply, message", [
    (401, {"code": 1000, "message": f"bad {KEY}"}, "Tripo rejected the API key"),
    (200, {"code": 1001, "message": f"bad {KEY}"}, "Tripo rejected the API key"),
    (403, {
        "code": 2010,
        "message": "You don't have enough credit to create this task",
        "suggestion": "Please purchase more credit",
    }, "You don't have enough credit to create this task. Please purchase more credit"),
    (403, {"code": 2010}, "Out of Tripo credits. Top up at tripo3d.ai"),
    (200, {"code": 2010}, "Out of Tripo credits. Top up at tripo3d.ai"),
    (403, {}, "Tripo refused the request: no permission, or not enough credits"),
    (429, {"code": 1007, "message": "Too many pending tasks"}, "Too many pending tasks"),
    (200, {"code": 1007}, "Tripo rate limit hit, wait and retry"),
    (429, {"code": 2000}, "Tripo concurrency limit hit, wait and retry"),
    (200, {"code": 2000, "message": "You have exceeded the limit of generation"},
     "You have exceeded the limit of generation"),
    (429, "not json", "Tripo rate limit hit, wait and retry"),
    (400, {"code": 2002, "message": "Prompt is invalid"}, "Prompt is invalid"),
    (404, {}, "Tripo could not find that resource"),
    (500, {}, "Tripo request failed. Try again later"),
])
def test_tripo_errors_are_readable_and_never_show_the_key(monkeypatch, status_code, reply, message):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, reply, status_code)
    result = addon.BlenderMCPServer().create_tripo_job(text_prompt="a teapot")
    assert result["error"] == message
    assert KEY not in str(result)
    assert len(posts) == 1


def test_tripo_network_failure_never_shows_the_key(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)

    def post(*_a, **_k):
        raise ConnectionError(f"boom Authorization: Bearer {KEY}")

    addon.requests.post = post
    result = addon.BlenderMCPServer().create_tripo_job(text_prompt="a teapot")
    assert "error" in result
    assert KEY not in str(result)


def test_tripo_poll_returns_model_url_and_quotes_task_id(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    seen = {}

    def get(url, headers=None, **_kwargs):
        seen["url"] = url
        seen["headers"] = headers
        return _JsonResponse({
            "code": 0,
            "data": {
                "task_id": "task/abc",
                "status": "success",
                "progress": 100,
                "output": {"pbr_model": "https://cdn.example/model.glb"},
            },
        })

    addon.requests.get = get
    result = addon.BlenderMCPServer().poll_tripo_job_status(task_id="task/abc")
    assert result["status"] == "success"
    assert result["model_url"] == "https://cdn.example/model.glb"
    assert seen["url"].endswith("/tasks/task%2Fabc")
    assert KEY not in str(result)
    assert seen["headers"]["Authorization"] == f"Bearer {KEY}"


def test_tripo_poll_failed_task_uses_error_message(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)

    def get(url, headers=None, **_kwargs):
        return _JsonResponse({
            "code": 0,
            "data": {
                "task_id": "task_abc",
                "status": "failed",
                "error_code": 2008,
                "error_message": "Content policy violation",
                "message": f"ignore {KEY}",
            },
        })

    addon.requests.get = get
    result = addon.BlenderMCPServer().poll_tripo_job_status(task_id="task_abc")
    assert result["status"] == "failed"
    assert result["error"] == "Content policy violation"
    assert KEY not in str(result)


def test_tripo_import_rejects_non_http_url(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    result = addon.BlenderMCPServer().import_generated_asset_tripo(name="Teapot", model_url="/tmp/model.glb")
    assert result["succeed"] is False


def test_tripo_import_downloads_glb(monkeypatch):
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
    result = server.import_generated_asset_tripo(name="Teapot", model_url="https://cdn.example/model.glb")
    assert result["succeed"] is True
    assert result["name"] == "Teapot"
    assert result["world_bounding_box"] == [[0, 0, 0], [1, 1, 1]]
    assert downloaded["bytes"] == b"glb-bytes"
    assert downloaded["user_agent"].startswith("mcp-for-blender/")


def test_tripo_commands_stay_unregistered_until_enabled(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch, blendermcp_tripo_api_key=KEY)
    server = addon.BlenderMCPServer()

    for command in ("create_tripo_job", "poll_tripo_job_status", "import_generated_asset_tripo"):
        blocked = server._execute_command_internal({"type": command, "params": {}})
        assert blocked["status"] == "error"
        assert "Unknown command type" in blocked["message"]

    scene.blendermcp_use_tripo = True
    posts = _capture_posts(addon)
    allowed = server._execute_command_internal({"type": "create_tripo_job", "params": {"text_prompt": "a teapot"}})
    assert allowed["status"] == "success"
    assert allowed["result"]["task_id"] == "task_abc"
    assert posts


def test_tripo_status_is_always_answered(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch)
    reply = addon.BlenderMCPServer()._execute_command_internal({"type": "get_tripo_status", "params": {}})
    assert reply["status"] == "success"
    assert reply["result"]["enabled"] is False
