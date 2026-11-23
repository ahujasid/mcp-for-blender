"""World Labs worlds: generate_gaussian_world's flow, and the addon's own-key and Premium
paths. Blender imports Gaussian splats from 5.3 on, so everything stays off below it.
"""
import asyncio
import base64
import importlib.util
import types

import pytest

from conftest import ROOT_ADDON as ADDON
from test_meshy import _install_bpy_stubs, _JsonResponse

from blender_mcp import generation
from blender_mcp.generation import GenerationError, Job

KEY = "wlt-super-secret-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


# ------------------------------------------------------------------ addon

def _load_addon(monkeypatch, version=(5, 3, 0), spz=True, **scene_attrs):
    monkeypatch.delenv("BLENDERMCP_WORLDLABS_API_KEY", raising=False)
    monkeypatch.delenv("BLENDERMCP_PREMIUM_LICENSE_KEY", raising=False)
    scene = types.SimpleNamespace(
        blendermcp_use_worldlabs=False,
        blendermcp_worldlabs_model="marble-1.1-plus",
        blendermcp_worldlabs_api_key="",
        blendermcp_use_meshy=False,
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
    bpy.app.version = version
    bpy.app.version_string = ".".join(str(part) for part in version)
    bpy.app.build_options = types.SimpleNamespace(io_spz=spz)
    spec = importlib.util.spec_from_file_location("blender_mcp_addon_worldlabs_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon, bpy, scene


def _ready(monkeypatch, **scene_attrs):
    attrs = {"blendermcp_use_worldlabs": True, "blendermcp_worldlabs_api_key": KEY, **scene_attrs}
    return _load_addon(monkeypatch, **attrs)


def _capture(addon, reply=None, status_code=200):
    calls = []

    def request(method, url, headers=None, json=None, **kwargs):
        calls.append({"method": method, "url": url, "headers": headers or {}, "json": json})
        return _JsonResponse(reply if reply is not None else {"operation_id": f"op_{len(calls)}"}, status_code)

    addon.requests.request = request
    return calls


def test_worldlabs_is_off_below_blender_5_3_even_through_premium(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, version=(5, 2, 2), blendermcp_use_worldlabs=True,
                                      blendermcp_worldlabs_api_key=KEY)
    server = addon.BlenderMCPServer()
    status = server.get_worldlabs_status()
    assert status["enabled"] is False
    assert "Blender 5.3" in status["message"] and "5.2.2" in status["message"]

    monkeypatch.setattr(addon, "premium_active", lambda: True)
    assert server.get_worldlabs_status()["enabled"] is False
    assert "worldlabs" not in addon.premium_enabled_generators()
    assert "Blender 5.3" in addon.premium_create_worldlabs_job(text_prompt="a courtyard")["error"]


def test_worldlabs_needs_the_spz_importer(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch, spz=False)
    assert addon.BlenderMCPServer().get_worldlabs_status()["enabled"] is False


def test_worldlabs_status_reports_sidebar_state(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    assert "World Labs" in server.get_worldlabs_status()["message"]

    scene.blendermcp_use_worldlabs = True
    missing_key = server.get_worldlabs_status()
    assert missing_key["enabled"] is False and "API key" in missing_key["message"]

    scene.blendermcp_worldlabs_api_key = KEY
    ready = server.get_worldlabs_status()
    assert ready["enabled"] is True and ready["model"] == "marble-1.1-plus"
    assert KEY not in str(ready)


def test_worldlabs_text_job_sends_the_sidebar_model(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon)
    reply = addon.BlenderMCPServer().create_worldlabs_job(text_prompt="a courtyard", name="C" * 80)
    assert reply == {"job_id": "op_1", "kind": "op", "model": "marble-1.1-plus"}
    call = calls[0]
    assert call["method"] == "POST" and call["url"] == "https://api.worldlabs.ai/marble/v1/worlds:generate"
    assert call["headers"]["WLT-Api-Key"] == KEY
    assert call["json"]["world_prompt"] == {"type": "text", "text_prompt": "a courtyard"}
    assert len(call["json"]["display_name"]) == 64


@pytest.mark.parametrize("sidebar, model", [
    ("marble-1.1-plus", "marble-1.1-plus"),
    ("marble-1.1", "marble-1.1"),
    # The draft model is no longer offered; a file saved with it gets the default.
    ("marble-1.0-draft", "marble-1.1-plus"),
])
def test_worldlabs_uses_the_sidebar_model(monkeypatch, sidebar, model):
    addon, _bpy, _scene = _ready(monkeypatch, blendermcp_worldlabs_model=sidebar)
    calls = _capture(addon)
    # An older server may still send quality; it's ignored.
    addon.BlenderMCPServer().create_worldlabs_job(text_prompt="a courtyard", quality="standard")
    assert calls[0]["json"]["model"] == model


def test_worldlabs_image_url_is_passed_and_local_files_go_inline(monkeypatch, tmp_path):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon)
    server = addon.BlenderMCPServer()

    server.create_worldlabs_job(image="https://example.com/room.jpg")
    assert calls[-1]["json"]["world_prompt"] == {
        "type": "image", "image_prompt": {"source": "uri", "uri": "https://example.com/room.jpg"}}

    photo = tmp_path / "room.png"
    photo.write_bytes(PNG)
    server.create_worldlabs_job(image=str(photo))
    content = calls[-1]["json"]["world_prompt"]["image_prompt"]
    assert content["source"] == "data_base64" and content["extension"] == "png"
    assert base64.b64decode(content["data_base64"]) == PNG


def test_worldlabs_folder_is_views_spread_around_the_place(monkeypatch, tmp_path):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon)
    (tmp_path / "b_back.jpg").write_bytes(JPEG)
    (tmp_path / "a_front.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("not an image")

    addon.BlenderMCPServer().create_worldlabs_job(image=str(tmp_path))
    prompt = calls[-1]["json"]["world_prompt"]
    assert prompt["type"] == "multi-image"
    assert [view["azimuth"] for view in prompt["multi_image_prompt"]] == [0, 180]
    assert prompt["multi_image_prompt"][0]["content"]["extension"] == "png"
    assert prompt["multi_image_prompt"][1]["content"]["extension"] == "jpg"


def test_worldlabs_refuses_bad_images_without_calling_the_api(monkeypatch, tmp_path):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon)
    server = addon.BlenderMCPServer()

    secret = tmp_path / "id_rsa.png"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "Not a PNG, JPEG or WebP" in server.create_worldlabs_job(image=str(secret))["error"]

    many = tmp_path / "many"
    many.mkdir()
    for index in range(5):
        (many / f"{index}.png").write_bytes(PNG)
    assert "up to 4 images" in server.create_worldlabs_job(image=str(many))["error"]
    assert "either a text prompt or an image" in server.create_worldlabs_job()["error"]
    assert not calls


@pytest.mark.parametrize("status_code, reply, needle", [
    (401, {"detail": "bad key"}, "rejected the API key"),
    (402, {}, "Out of World Labs API credits"),
    (400, {"detail": "Prompt violates the content policy"}, "content policy"),
    (422, {"detail": [{"msg": "field required"}]}, "status 422"),
])
def test_worldlabs_errors_are_readable_and_never_show_the_key(monkeypatch, status_code, reply, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    _capture(addon, reply, status_code)
    error = addon.BlenderMCPServer().create_worldlabs_job(text_prompt="a courtyard")["error"]
    assert needle in error and KEY not in error


@pytest.mark.parametrize("operation, expected", [
    ({"done": False, "metadata": {"progress": {"status": "IN_PROGRESS", "description": "Generating world"}}},
     {"status": "IN_PROGRESS", "progress": "Generating world"}),
    ({"done": True, "error": {"code": 3, "message": "Generation failed"}},
     {"status": "FAILED", "error": "Generation failed"}),
    ({"done": True, "error": None, "response": {"id": "w1"}}, {"status": "SUCCEEDED"}),
])
def test_worldlabs_poll_maps_the_operation(monkeypatch, operation, expected):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon, operation)
    assert addon.BlenderMCPServer().poll_worldlabs_job(job_id="op/1", kind="op") == expected
    assert calls[0]["method"] == "GET" and calls[0]["url"].endswith("/operations/op%2F1")


def test_worldlabs_own_key_poll_refuses_a_premium_job(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    calls = _capture(addon)
    assert "through Premium" in addon.BlenderMCPServer().poll_worldlabs_job(job_id="p1", kind="prm")["error"]
    assert not calls


WORLD = {"done": True, "error": None, "response": {"model": "marble-1.1-plus", "assets": {
    "splats": {"spz_urls": {"100k": "https://cdn/w_100k.spz", "500k": "https://cdn/w_500k.spz",
                            "full_res": "https://cdn/w.spz"},
               "semantics_metadata": {"metric_scale_factor": 1.4, "ground_plane_offset": 1.6}},
    "mesh": {"collider_mesh_url": "https://cdn/collider.glb"},
    "imagery": {"pano_url": "https://cdn/pano.png"},
}}}


def _capture_import(monkeypatch, addon):
    imported = []
    monkeypatch.setattr(addon, "import_world_assets", lambda *args: imported.append(args) or {"succeed": True})
    return imported


def test_worldlabs_import_picks_the_resolution_and_passes_the_scale(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    _capture(addon, WORLD)
    imported = _capture_import(monkeypatch, addon)
    reply = addon.BlenderMCPServer().import_worldlabs_world("Courtyard", job_id="op_1", kind="op",
                                                            resolution="full_res")
    assert reply["model"] == "Marble 1.1 Plus"
    assert imported == [("Courtyard", "https://cdn/w.spz", "full_res",
                         {"metric_scale_factor": 1.4, "ground_plane_offset": 1.6},
                         "https://cdn/collider.glb", "https://cdn/pano.png")]


def test_worldlabs_import_falls_back_to_a_size_it_has(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    only = {**WORLD, "response": {"assets": {"splats": {"spz_urls": {"100k": "https://cdn/w_100k.spz"}}}}}
    _capture(addon, only)
    imported = _capture_import(monkeypatch, addon)
    addon.BlenderMCPServer().import_worldlabs_world("Courtyard", job_id="op_1", kind="op")
    assert imported[0][1:3] == ("https://cdn/w_100k.spz", "100k")


def test_worldlabs_import_refuses_an_unfinished_world(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    _capture(addon, {"done": False})
    reply = addon.BlenderMCPServer().import_worldlabs_world("Courtyard", job_id="op_1", kind="op")
    assert reply["succeed"] is False and "isn't finished" in reply["error"]


def test_worldlabs_commands_stay_unregistered_until_enabled(monkeypatch):
    addon, _bpy, scene = _load_addon(monkeypatch, blendermcp_worldlabs_api_key=KEY)
    server = addon.BlenderMCPServer()
    for command in ("create_worldlabs_job", "poll_worldlabs_job", "import_worldlabs_world"):
        assert "Unknown command type" in server._execute_command_internal({"type": command, "params": {}})["message"]
    assert server._execute_command_internal({"type": "get_worldlabs_status", "params": {}})["status"] == "success"

    scene.blendermcp_use_worldlabs = True
    _capture(addon)
    reply = server._execute_command_internal({"type": "create_worldlabs_job",
                                              "params": {"text_prompt": "a courtyard"}})
    assert reply["result"]["job_id"] == "op_1"


# ------------------------------------------------------------------ addon, Premium

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


def test_worldlabs_runs_through_premium_without_a_key(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    calls = _premium(monkeypatch, addon)
    server = addon.BlenderMCPServer()
    assert "worldlabs" in addon.premium_enabled_generators()

    reply = server._execute_command_internal({"type": "create_worldlabs_job",
                                              "params": {"text_prompt": "a courtyard"}})
    assert reply["result"]["job_id"] == "pjob_1" and reply["result"]["kind"] == "prm"
    payload = calls[-1][2]
    assert (payload["model"], payload["quality"], payload["prompt"]) == ("worldlabs", "high", "a courtyard")


def test_worldlabs_premium_is_always_high_quality(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    calls = _premium(monkeypatch, addon)
    addon.premium_create_worldlabs_job(image="https://example.com/room.jpg", quality="standard")
    assert calls[-1][2]["quality"] == "high"
    assert calls[-1][2]["image_url"] == "https://example.com/room.jpg"


def test_worldlabs_premium_on_hobby_says_pro_without_asking_the_server(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    calls = _premium(monkeypatch, addon)
    addon._premium_status.update({"tier": "hobby", "premium": {"total": 0, "used": 0, "remaining": 0}})
    reply = addon.premium_create_worldlabs_job(text_prompt="a courtyard")
    assert reply["code"] == "NOT_IN_PLAN" and "Pro" in reply["message"]
    assert not calls


def test_worldlabs_premium_refuses_a_folder(monkeypatch, tmp_path):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    calls = _premium(monkeypatch, addon)
    assert "one image" in addon.premium_create_worldlabs_job(image=str(tmp_path))["error"]
    assert not calls


@pytest.mark.parametrize("job, expected", [
    ({"status": "running", "provider_status": "IN_PROGRESS"}, {"status": "IN_PROGRESS", "progress": "IN_PROGRESS"}),
    ({"status": "succeeded"}, {"status": "SUCCEEDED"}),
    ({"status": "failed", "error": "World generation failed"},
     {"status": "FAILED", "error": "World generation failed. This attempt wasn't counted."}),
])
def test_worldlabs_premium_poll(monkeypatch, job, expected):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    _premium(monkeypatch, addon, {"pj1": job})
    assert addon.premium_poll_worldlabs_job(job_id="pj1", kind="prm") == expected
    assert "own World Labs API key" in addon.premium_poll_worldlabs_job(job_id="op_1", kind="op")["error"]


def test_worldlabs_premium_import_reads_the_job_files(monkeypatch):
    addon, _bpy, _scene = _load_addon(monkeypatch, blendermcp_use_worldlabs=True)
    meta = {"metric_scale_factor": 1.4, "ground_plane_offset": 1.6}
    files = [
        {"role": "splats", "format": "spz", "url": "https://cdn/w_500k.spz", "resolution": "500k", "metadata": meta},
        {"role": "splats", "format": "spz", "url": "https://cdn/w_100k.spz", "resolution": "100k", "metadata": meta},
        {"role": "collider", "format": "glb", "url": "https://cdn/collider.glb"},
        {"role": "panorama", "format": "png", "url": "https://cdn/pano.png"},
    ]
    _premium(monkeypatch, addon, {"pj1": {"status": "succeeded", "files": files}})
    imported = _capture_import(monkeypatch, addon)

    assert addon.premium_import_worldlabs("Courtyard", job_id="pj1", kind="prm", resolution="100k") == {
        "succeed": True, "model": "Marble 1.1"}
    addon.premium_import_worldlabs("Courtyard", job_id="pj1", kind="prm", resolution="full_res")
    assert imported[0] == ("Courtyard", "https://cdn/w_100k.spz", "100k", meta,
                           "https://cdn/collider.glb", "https://cdn/pano.png")
    assert imported[1][1:3] == ("https://cdn/w_500k.spz", "500k")


# ------------------------------------------------------------------ flow

class FakeAddon:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        reply = self.replies[command]
        if callable(reply):
            return reply(params)
        if isinstance(reply, list):
            return reply.pop(0) if len(reply) > 1 else reply[0]
        return reply


IMPORTED = {"succeed": True, "name": "Courtyard", "model": "Marble 1.1 Plus", "splats": "Courtyard Splats", "collider": "Courtyard Collider",
            "background": "Courtyard Sky", "resolution": "500k", "metric": True, "viewpoint": [0.0, 0.0, 1.6],
            "world_bounding_box": [[-5, -4, 0], [6, 7, 3]], "notes": []}


def _run(send, job, wait=100, options=None):
    async def no_sleep(_):
        pass

    ticks = iter(range(0, 10_000, 5))
    return asyncio.run(generation.wait_and_import(send, job, wait, sleep=no_sleep, clock=lambda: next(ticks),
                                                  import_options=options))


def test_world_handles_parse_and_resume_with_generate_gaussian_world():
    job = Job.parse("worldlabs:op:abc-123", "Courtyard")
    assert job == Job("worldlabs", "op", "abc-123", "Courtyard") and job.is_world
    assert 'generate_gaussian_world(job="worldlabs:op:abc-123"' in job.resume_hint
    assert "generate_3d" in Job("tripo", "rid", "r1", "X").resume_hint
    with pytest.raises(GenerationError):
        Job.parse("worldlabs:task:abc", "X")


def test_world_flow_polls_then_imports_at_the_asked_resolution():
    send = FakeAddon({
        "create_worldlabs_job": {"job_id": "op_1", "kind": "op"},
        "poll_worldlabs_job": [{"status": "IN_PROGRESS", "progress": "Generating world"}, {"status": "SUCCEEDED"}],
        "import_worldlabs_world": IMPORTED,
    })
    job = generation.submit_world(send, "Courtyard", "a courtyard", None)
    assert send.calls[0] == ("create_worldlabs_job", {"text_prompt": "a courtyard", "image": None,
                                                      "name": "Courtyard"})
    assert _run(send, job, options={"resolution": "full_res"}) == (True, IMPORTED)
    assert send.calls[-1] == ("import_worldlabs_world", {"name": "Courtyard", "job_id": "op_1", "kind": "op",
                                                         "resolution": "full_res"})


def test_world_flow_returns_the_progress_when_time_runs_out():
    send = FakeAddon({"poll_worldlabs_job": {"status": "IN_PROGRESS", "progress": "Generating world"}})
    assert _run(send, Job("worldlabs", "op", "op_1", "C"), wait=12) == (False, "Generating world")


def test_world_flow_relays_a_failed_world():
    send = FakeAddon({"poll_worldlabs_job": {"status": "FAILED", "error": "Content policy"}})
    with pytest.raises(GenerationError, match="Content policy"):
        _run(send, Job("worldlabs", "prm", "pj1", "C"))


# ------------------------------------------------------------------ tool

class _Ctx:
    async def report_progress(self, *_):
        pass


class _Blender:
    def __init__(self, status):
        self.status = status

    def send_command(self, command, params=None):
        assert command == "get_worldlabs_status"
        return self.status


def test_generate_gaussian_world_relays_why_world_labs_is_off(monkeypatch):
    from blender_mcp import server

    message = "World Labs worlds need Blender 5.3 or newer, which imports Gaussian splats; this is Blender 5.2.2."
    monkeypatch.setattr(server, "get_blender_connection", lambda: _Blender({"enabled": False, "message": message}))
    assert asyncio.run(server.generate_gaussian_world(_Ctx(), prompt="a courtyard")) == f"Error: {message}"


def test_generate_gaussian_world_imports_and_describes_the_world(monkeypatch):
    from blender_mcp import server

    send = FakeAddon({
        "create_worldlabs_job": {"job_id": "op_1", "kind": "op"},
        "poll_worldlabs_job": {"status": "SUCCEEDED"},
        "import_worldlabs_world": IMPORTED,
    })
    monkeypatch.setattr(server, "get_blender_connection", lambda: _Blender({"enabled": True}))
    monkeypatch.setattr(server, "_generation_send", send)
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(None))
    reply = asyncio.run(server.generate_gaussian_world(_Ctx(), prompt="a sunlit courtyard", resolution="100k"))
    assert "World Labs Marble 1.1 Plus:" in reply
    assert "'Courtyard Splats'" in reply and "'Courtyard Collider'" in reply
    assert "metres with the ground at z=0" in reply and "[0.0, 0.0, 1.6]" in reply
    assert send.calls[-1][1]["resolution"] == "100k"
    assert send.calls[0][1]["name"] == "ASunlitCourtyard"


def test_generate_gaussian_world_and_generate_3d_refuse_each_others_jobs():
    from blender_mcp import server

    assert "generate_3d(job=" in asyncio.run(server.generate_gaussian_world(_Ctx(), job="tripo:rid:r1"))
    assert "generate_gaussian_world(job=" in asyncio.run(server.generate_3d(_Ctx(), job="worldlabs:op:op_1"))


def test_generate_gaussian_world_validates_before_reaching_blender():
    from blender_mcp import server

    assert "resolution" in asyncio.run(server.generate_gaussian_world(_Ctx(), prompt="x", resolution="2m"))
