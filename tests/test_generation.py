"""generate_3d drives every provider through one submit/poll/import flow.

A fake addon replays each provider's real reply shapes, so these pin the
shapes the flow depends on: Premium and fal request ids, Rodin main-site
subscription keys, Tencent-style Hunyuan responses, and Meshy's preview then
refine tasks.
"""

import asyncio

import pytest

from blender_mcp import generation
from blender_mcp.generation import GenerationError, Job, choose_provider


class FakeAddon:
    def __init__(self, replies):
        # command -> reply, a list of replies consumed in order, or a function of the params
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


def _run(send, job, wait=100, pending=None):
    async def no_sleep(_):
        pass

    ticks = iter(range(0, 10_000, 5))
    return asyncio.run(generation.wait_and_import(send, job, wait, sleep=no_sleep, clock=lambda: next(ticks),
                                                  pending=pending))


# -------------------------------------------------------------- provider choice

def test_auto_prefers_premium_generators():
    assert choose_provider("auto", ["hyper3d", "tripo"], {}) == ("tripo", True)
    assert choose_provider("auto", ["hunyuan3d"], {"hyper3d": True}) == ("hunyuan3d", True)


def test_auto_falls_back_to_own_keys():
    assert choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": True}) == ("hyper3d", False)


def test_nothing_enabled_points_at_setup():
    with pytest.raises(GenerationError, match="No 3D generator"):
        choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": False})


def test_tripo_needs_premium():
    with pytest.raises(GenerationError, match="Premium"):
        choose_provider("tripo", [], {"hunyuan3d": True})


def test_explicit_provider_must_be_on_in_premium():
    with pytest.raises(GenerationError, match="not switched on"):
        choose_provider("hyper3d", ["tripo"], {})


# ------------------------------------------------------------------ flows

def test_tripo_submit_poll_import():
    send = FakeAddon({
        "create_tripo_job": {"request_id": "r1", "status": "IN_QUEUE"},
        "poll_tripo_job_status": [{"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
        "import_generated_asset_tripo": {"succeed": True},
    })
    job = generation.submit(send, "tripo", "Chest", "a chest", None, "high", None, True)
    assert job.handle == "tripo:rid:r1"
    assert send.calls[0] == ("create_tripo_job", {"text_prompt": "a chest", "image": None, "quality": "high"})
    assert _run(send, job) == (True, "Chest")
    assert send.calls[-1] == ("import_generated_asset_tripo", {"request_id": "r1", "name": "Chest"})


def test_premium_error_codes_are_relayed_not_retried():
    send = FakeAddon({"create_tripo_job": {"code": "QUOTA_EXHAUSTED", "message": "You've used all generations."}})
    with pytest.raises(GenerationError, match="used all generations.*don't retry"):
        generation.submit(send, "tripo", "X", "x", None, None, None, True)


def test_hunyuan_polls_tencent_shapes_and_imports_the_glb():
    send = FakeAddon({
        "create_hunyuan_job": {"Response": {"JobId": "42"}},
        "poll_hunyuan_job_status": [
            {"Response": {"Status": "RUN"}},
            {"Response": {"Status": "DONE", "ResultFile3Ds": [
                {"Type": "OBJ", "Url": "https://x/model.zip"}, {"Type": "GLB", "Url": "https://x/model.glb"}]}},
        ],
        "import_generated_asset_hunyuan": {"succeed": True},
    })
    job = generation.submit(send, "hunyuan3d", "Lamp", "a lamp", None, "high", None, supports_quality=False)
    assert job.handle == "hunyuan3d:job:job_42"
    assert "quality" not in send.calls[0][1]  # older addons reject unknown arguments
    assert _run(send, job) == (True, "Lamp")
    assert send.calls[-1] == ("import_generated_asset_hunyuan", {"name": "Lamp", "zip_file_url": "https://x/model.glb"})


def test_hunyuan_failure_says_why():
    send = FakeAddon({"poll_hunyuan_job_status": {"Response": {"Status": "FAIL", "ErrorMessage": "bad prompt"}}})
    with pytest.raises(GenerationError, match="bad prompt"):
        _run(send, Job("hunyuan3d", "job", "job_1", "X"))


def test_rodin_main_site_uses_subscription_key_then_task_uuid():
    send = FakeAddon({
        "create_rodin_job": {"submit_time": True, "uuid": "u1", "jobs": {"subscription_key": "s1"}},
        "poll_rodin_job_status": [{"status_list": ["Done", "Generating"]}, {"status_list": ["Done", "Done"]}],
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", "a car", None, None, [2.0, 1.0, 1.0], True)
    assert job.handle == "hyper3d:main:u1|s1"
    assert send.calls[0][1]["bbox_condition"] == [100, 50, 50]
    assert _run(send, job) == (True, "Car")
    assert ("poll_rodin_job_status", {"subscription_key": "s1"}) in send.calls
    assert send.calls[-1] == ("import_generated_asset", {"task_uuid": "u1", "name": "Car"})


def test_rodin_fal_and_premium_use_request_id():
    send = FakeAddon({
        "create_rodin_job": {"request_id": "q1"},
        "poll_rodin_job_status": {"status": "COMPLETED"},
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", None, "https://img/car.png", None, None, True)
    assert send.calls[0][1]["images"] == ["https://img/car.png"]
    assert send.calls[0][1]["text_prompt"] is None
    assert _run(send, job) == (True, "Car")
    assert send.calls[-1] == ("import_generated_asset", {"request_id": "q1", "name": "Car"})


def test_running_out_of_time_returns_a_resumable_handle():
    send = FakeAddon({"poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    job = Job("tripo", "rid", "r9", "Robot")
    imported, detail = _run(send, job, wait=20)
    assert not imported and detail == "IN_PROGRESS"
    assert Job.parse(job.handle, "Robot") == job


def test_failed_import_is_an_error():
    send = FakeAddon({
        "poll_tripo_job_status": {"status": "COMPLETED"},
        "import_generated_asset_tripo": {"succeed": False, "error": "download failed"},
    })
    with pytest.raises(GenerationError, match="download failed"):
        _run(send, Job("tripo", "rid", "r1", "X"))


def test_bad_handles_are_rejected():
    for handle in ("", "tripo", "meshy:rid:1", "tripo:rid:"):
        with pytest.raises(GenerationError):
            Job.parse(handle, "X")


def test_default_name_comes_from_the_prompt():
    assert generation.default_name("a weathered wooden treasure chest") == "AWeatheredWooden"
    assert generation.default_name(None) == "Generated"


# -------------------------------------------------------------- stranded jobs

def test_errors_after_submit_keep_the_handle():
    class FlakyAddon(FakeAddon):
        def __call__(self, command, params):
            raise ConnectionError("Socket timeout while waiting for response from Blender")

    job = Job("tripo", "rid", "r7", "Chest")
    with pytest.raises(GenerationError) as e:
        _run(FlakyAddon({}), job)
    assert job.handle in str(e.value) and "Socket timeout" in str(e.value)


def test_pending_jobs_survive_on_disk_until_settled(tmp_path):
    path = tmp_path / "pending-generations.json"
    job = Job("tripo", "rid", "r1", "Robot")
    key = generation.request_key("tripo", "a robot", None, "high", None)
    generation.Pending(path).add(key, job)

    # A fresh instance, as after a restart, still finds it; a different request doesn't.
    assert generation.Pending(path).find(key) == job
    assert generation.Pending(path).find(generation.request_key("tripo", "a robot", None, None, None)) is None

    send = FakeAddon({"poll_tripo_job_status": {"status": "COMPLETED"},
                      "import_generated_asset_tripo": {"succeed": True}})
    asyncio.run(generation.wait_and_import(send, job, 100, sleep=lambda _: asyncio.sleep(0),
                                           pending=generation.Pending(path)))
    assert generation.Pending(path).find(key) is None


def test_failed_jobs_leave_pending_but_interrupted_ones_stay(tmp_path):
    path = tmp_path / "pending.json"
    failed, cut = Job("tripo", "rid", "f1", "A"), Job("tripo", "rid", "c1", "B")
    generation.Pending(path).add("kf", failed)
    generation.Pending(path).add("kc", cut)

    send = FakeAddon({"poll_tripo_job_status": {"status": "FAILED", "error": "bad input"}})
    with pytest.raises(GenerationError):
        asyncio.run(generation.wait_and_import(send, failed, 100, pending=generation.Pending(path)))
    send = FakeAddon({"poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    asyncio.run(generation.wait_and_import(send, cut, 1, pending=generation.Pending(path)))

    assert generation.Pending(path).find("kf") is None
    assert generation.Pending(path).find("kc") == cut


def test_old_or_unreadable_pending_entries_are_ignored(tmp_path):
    path = tmp_path / "pending.json"
    now = [1_000_000.0]
    pending = generation.Pending(path, clock=lambda: now[0])
    pending.add("k", Job("tripo", "rid", "r1", "A"))
    now[0] += generation.PENDING_MAX_AGE_S + 1
    assert pending.find("k") is None

    path.write_text("not json")
    assert generation.Pending(path).find("k") is None
    generation.Pending(path).add("k2", Job("tripo", "rid", "r2", "B"))
    assert generation.Pending(path).find("k2") is not None
    assert generation.Pending(None).find("k2") is None


def test_repeating_an_unfinished_request_resumes_instead_of_paying_again(tmp_path, monkeypatch):
    from blender_mcp import server

    send = FakeAddon({"create_tripo_job": {"request_id": "r1"},
                      "poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    path = tmp_path / "pending.json"
    monkeypatch.setattr(server, "get_blender_connection", lambda: None)
    monkeypatch.setattr(server, "_premium_generators", lambda _: ["tripo"])
    monkeypatch.setattr(server, "_addon_protocol", lambda: 13)
    monkeypatch.setattr(server, "_generation_send", send)
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(path))
    monkeypatch.setattr(generation, "POLL_INTERVAL_S", 0.0)

    class Ctx:
        async def report_progress(self, *_):
            pass

    async def call(**kw):
        return await server.generate_3d(Ctx(), image="/tmp/ref.png", quality="high", wait_seconds=10, **kw)

    monkeypatch.setattr(generation, "submit", lambda send_, *a, **k: Job("tripo", "rid", send_("create_tripo_job", {})["request_id"], "Ref"))
    first = asyncio.run(call())
    second = asyncio.run(call())
    assert 'job="tripo:rid:r1"' in first and 'job="tripo:rid:r1"' in second
    assert "resumed" in second
    assert [c for c, _ in send.calls].count("create_tripo_job") == 1


# ------------------------------------------------------------------- Meshy
#
# Text is two paid Meshy tasks: a preview (mesh), then a refine (texture). One
# preview must never get a second refine, whichever way the flow is resumed.

def _meshy(statuses, **replies):
    """A fake addon whose Meshy tasks report `statuses[task_id]` in order (the
    last one repeats). Edit the lists between calls to move a task along."""
    def poll(params):
        queue = statuses[params["task_id"]]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, dict):
            return reply
        result = {"status": reply, "progress": 100 if reply == "SUCCEEDED" else 50}
        if reply == "SUCCEEDED" and params["kind"] != "t2p":
            result["model_url"] = f"https://assets.meshy.ai/{params['task_id']}.glb"
        return result

    return FakeAddon({
        "create_meshy_job": {"task_id": "p1", "kind": "t2p", "model": "latest"},
        "refine_meshy_job": {"task_id": "r1", "kind": "t2r"},
        "poll_meshy_job_status": poll,
        "import_generated_asset_meshy": {"succeed": True},
        **replies,
    })


def _count(send, command):
    return [c for c, _ in send.calls].count(command)


def _start_meshy(send, path, prompt="a teapot"):
    """Submit as generate_3d does: the job is written down before any polling."""
    job = generation.submit(send, "meshy", "Teapot", prompt, None, None, None, True)
    key = generation.request_key("meshy", prompt, None, None, None)
    generation.Pending(path).add(key, job)
    return job, key


def test_meshy_text_previews_refines_once_and_imports_the_refine(tmp_path):
    path = tmp_path / "pending.json"
    send = _meshy({"p1": ["IN_PROGRESS", "SUCCEEDED"], "r1": ["PENDING", "IN_PROGRESS", "SUCCEEDED"]})
    job, key = _start_meshy(send, path)
    assert job.handle == "meshy:t2p:p1"
    assert send.calls[0] == ("create_meshy_job", {"text_prompt": "a teapot", "image": None})

    assert _run(send, job, pending=generation.Pending(path)) == (True, "Teapot")

    assert _count(send, "refine_meshy_job") == 1
    assert ("refine_meshy_job", {"preview_task_id": "p1"}) in send.calls
    assert ("poll_meshy_job_status", {"task_id": "r1", "kind": "t2r"}) in send.calls
    assert send.calls[-1] == ("import_generated_asset_meshy",
                              {"name": "Teapot", "model_url": "https://assets.meshy.ai/r1.glb"})
    assert job.handle == "meshy:t2r:r1"
    assert generation.Pending(path).find(key) is None


def test_meshy_quality_and_bbox_are_not_sent():
    send = _meshy({})
    generation.submit(send, "meshy", "Teapot", "a teapot", None, "high", [1, 1, 1], True)
    assert send.calls[0] == ("create_meshy_job", {"text_prompt": "a teapot", "image": None})


def test_meshy_timeout_mid_preview_then_resume_refines_once(tmp_path):
    path = tmp_path / "pending.json"
    statuses = {"p1": ["IN_PROGRESS"], "r1": ["SUCCEEDED"]}
    send = _meshy(statuses)
    job, _key = _start_meshy(send, path)

    imported, _detail = _run(send, job, wait=20, pending=generation.Pending(path))
    assert not imported and job.handle == "meshy:t2p:p1"
    assert _count(send, "refine_meshy_job") == 0

    statuses["p1"] = ["SUCCEEDED"]
    resumed = generation.Job.parse("meshy:t2p:p1", "Teapot")
    assert _run(send, resumed, pending=generation.Pending(path)) == (True, "Teapot")
    assert _count(send, "refine_meshy_job") == 1


def test_meshy_resume_with_the_old_preview_handle_after_refine_does_not_refine_again(tmp_path):
    path = tmp_path / "pending.json"
    statuses = {"p1": ["SUCCEEDED"], "r1": ["IN_PROGRESS"]}
    send = _meshy(statuses)
    job, key = _start_meshy(send, path)

    imported, _detail = _run(send, job, wait=20, pending=generation.Pending(path))
    assert not imported
    assert _count(send, "refine_meshy_job") == 1
    # The hint the caller hands back now names the refine, and so does a retry of the request.
    assert job.handle == "meshy:t2r:r1" and 'job="meshy:t2r:r1"' in job.resume_hint
    assert generation.Pending(path).find(key).handle == "meshy:t2r:r1"

    # The agent kept the preview handle from an earlier reply. A fresh Pending
    # reads the file as a restarted server would.
    statuses["r1"] = ["SUCCEEDED"]
    stale = generation.Job.parse("meshy:t2p:p1", "Teapot")
    assert _run(send, stale, pending=generation.Pending(path)) == (True, "Teapot")
    assert _count(send, "refine_meshy_job") == 1
    assert send.calls[-1][1]["model_url"] == "https://assets.meshy.ai/r1.glb"


def test_meshy_stale_preview_handle_after_import_does_not_refine_again(tmp_path):
    path = tmp_path / "pending.json"
    send = _meshy({"p1": ["SUCCEEDED"], "r1": ["SUCCEEDED"]})
    job, key = _start_meshy(send, path)
    assert _run(send, job, pending=generation.Pending(path)) == (True, "Teapot")
    assert generation.Pending(path).find(key) is None

    stale = generation.Job.parse("meshy:t2p:p1", "Teapot")
    assert _run(send, stale, pending=generation.Pending(path)) == (True, "Teapot")
    assert _count(send, "refine_meshy_job") == 1


def test_meshy_refine_records_outlive_completion_but_age_out(tmp_path):
    path = tmp_path / "pending.json"
    now = [1_000_000.0]
    pending = generation.Pending(path, clock=lambda: now[0])
    job = Job("meshy", "t2r", "r1", "Teapot")
    pending.add("k", job)
    pending.record_refine("p1", job.handle)

    pending.remove(job)
    assert pending.find("k") is None
    assert pending.refine_for("p1") == "meshy:t2r:r1"
    assert pending.refine_for("p2") is None

    now[0] += generation.PENDING_MAX_AGE_S + 1
    assert pending.refine_for("p1") is None


def test_meshy_resent_request_resumes_across_restarts_without_paying_again(tmp_path, monkeypatch):
    from blender_mcp import server

    statuses = {"p1": ["IN_PROGRESS"], "r1": ["IN_PROGRESS"]}
    send = _meshy(statuses)
    path = tmp_path / "pending.json"
    monkeypatch.setattr(server, "get_blender_connection", lambda: None)
    monkeypatch.setattr(server, "_premium_generators", lambda _: [])
    monkeypatch.setattr(server, "_own_key_generators", lambda _: {"meshy": True})
    monkeypatch.setattr(server, "_generation_send", send)
    # A new Pending per call, as a restarted server would read the file.
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(path))
    # One poll per call, then the reply.
    monkeypatch.setattr(generation, "POLL_INTERVAL_S", 1000.0)

    class Ctx:
        async def report_progress(self, *_):
            pass

    def call():
        return asyncio.run(server.generate_3d(Ctx(), prompt="a ceramic teapot", provider="meshy", wait_seconds=10))

    first = call()
    assert 'job="meshy:t2p:p1"' in first

    statuses["p1"] = ["SUCCEEDED"]
    second = call()
    assert "resumed" in second and 'job="meshy:t2r:r1"' in second

    third = call()
    assert "resumed" in third and 'job="meshy:t2r:r1"' in third

    statuses["r1"] = ["SUCCEEDED"]
    fourth = call()
    assert "Generated and imported" in fourth

    assert _count(send, "create_meshy_job") == 1
    assert _count(send, "refine_meshy_job") == 1
    assert _count(send, "import_generated_asset_meshy") == 1


@pytest.mark.parametrize("image", ["https://example.com/teapot.png", "/Users/me/Pictures/teapot.png"])
def test_meshy_image_is_one_task_then_import(image):
    send = _meshy({"i1": ["IN_PROGRESS", "SUCCEEDED"]},
                  create_meshy_job={"task_id": "i1", "kind": "i2m", "model": "latest"})
    job = generation.submit(send, "meshy", "Teapot", None, image, None, None, True)
    assert job.handle == "meshy:i2m:i1"
    # The addon reads a local path and sends it as a data URI.
    assert send.calls[0] == ("create_meshy_job", {"text_prompt": None, "image": image})

    assert _run(send, job) == (True, "Teapot")
    assert _count(send, "refine_meshy_job") == 0
    assert ("poll_meshy_job_status", {"task_id": "i1", "kind": "i2m"}) in send.calls
    assert send.calls[-1] == ("import_generated_asset_meshy",
                              {"name": "Teapot", "model_url": "https://assets.meshy.ai/i1.glb"})


def test_meshy_failed_preview_says_why_and_clears_pending(tmp_path):
    path = tmp_path / "pending.json"
    send = _meshy({"p1": [{"status": "FAILED", "progress": 10, "error": "Prompt was flagged"}]})
    job, key = _start_meshy(send, path)

    with pytest.raises(GenerationError, match="Prompt was flagged"):
        _run(send, job, pending=generation.Pending(path))
    assert _count(send, "refine_meshy_job") == 0
    assert generation.Pending(path).find(key) is None


def test_meshy_failed_refine_says_why_and_clears_pending(tmp_path):
    path = tmp_path / "pending.json"
    send = _meshy({"p1": ["SUCCEEDED"], "r1": [{"status": "CANCELED", "progress": 0, "error": "CANCELED"}]})
    job, key = _start_meshy(send, path)

    with pytest.raises(GenerationError, match="CANCELED"):
        _run(send, job, pending=generation.Pending(path))
    assert generation.Pending(path).find(key) is None


@pytest.mark.parametrize("message", [
    "Meshy rejected the API key",
    "Out of Meshy credits. Top up at meshy.ai",
    "Meshy rate limit hit, wait and retry",
])
def test_meshy_api_errors_are_relayed_without_retrying(tmp_path, message):
    send = _meshy({}, create_meshy_job={"error": message})
    with pytest.raises(GenerationError, match=message):
        generation.submit(send, "meshy", "X", "a teapot", None, None, None, True)
    assert _count(send, "create_meshy_job") == 1

    # At refine time the preview is done and paid for: keep it resumable.
    path = tmp_path / "pending.json"
    send = _meshy({"p1": ["SUCCEEDED"]}, refine_meshy_job={"error": message})
    job, key = _start_meshy(send, path)
    with pytest.raises(GenerationError, match=message) as e:
        _run(send, job, pending=generation.Pending(path))
    assert 'job="meshy:t2p:p1"' in str(e.value)
    assert _count(send, "refine_meshy_job") == 1
    assert generation.Pending(path).find(key) is not None


def test_meshy_long_prompts_are_rejected_before_sending():
    send = _meshy({})
    with pytest.raises(GenerationError, match="800"):
        generation.submit(send, "meshy", "X", "x" * 801, None, None, None, True)
    assert send.calls == []


def test_meshy_handles_name_a_meshy_task_kind():
    for kind in ("t2p", "t2r", "i2m"):
        assert generation.Job.parse(f"meshy:{kind}:abc", "X") == Job("meshy", kind, "abc", "X")
    with pytest.raises(GenerationError):
        generation.Job.parse("meshy:task:abc", "X")


def test_meshy_provider_choice():
    assert choose_provider("meshy", [], {"meshy": True}) == ("meshy", False)
    assert choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": False, "meshy": True}) == ("meshy", False)
    # Last in the own-key order: Sid decides where it belongs.
    assert choose_provider("auto", [], {"hunyuan3d": True, "hyper3d": True, "meshy": True}) == ("hunyuan3d", False)
    with pytest.raises(GenerationError, match="Meshy API key"):
        choose_provider("meshy", [], {"meshy": False})
    # Own key only: never picked by, or available through, Premium.
    assert choose_provider("auto", ["hyper3d"], {"meshy": True}) == ("hyper3d", True)
    with pytest.raises(GenerationError, match="own Meshy API key only"):
        choose_provider("meshy", ["hyper3d"], {})


def test_nothing_enabled_mentions_meshy():
    with pytest.raises(GenerationError, match="Meshy"):
        choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": False, "meshy": False})


def test_meshy_key_never_crosses_the_mcp_side(tmp_path):
    path = tmp_path / "pending.json"
    send = _meshy({"p1": ["SUCCEEDED"], "r1": ["SUCCEEDED"]})
    job, _key = _start_meshy(send, path)
    _run(send, job, pending=generation.Pending(path))
    for command, params in send.calls:
        assert not any("key" in name.lower() for name in params), (command, params)
