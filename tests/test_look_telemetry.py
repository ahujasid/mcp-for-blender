"""Every look, whatever its mode, reaches telemetry with its mode and its image."""

import asyncio

from blender_mcp import server, telemetry_decorator


class _Telemetry:
    def __init__(self):
        self.uploads = []
        self.events = []

    def upload_screenshot(self, image_bytes, prefix):
        self.uploads.append((image_bytes, prefix))
        return f"storage:bucket/{prefix}_{len(self.uploads)}.png"

    def record_event(self, **event):
        self.events.append(event)


def _capture(monkeypatch):
    telemetry = _Telemetry()
    steps = []
    monkeypatch.setattr(telemetry_decorator, "get_telemetry", lambda: telemetry)
    monkeypatch.setattr(telemetry_decorator, "_record_observe_step",
                        lambda tool, **step: steps.append((tool, step)))
    return telemetry, steps


def _script_png(monkeypatch, png=b"\x89PNG render"):
    def fake_run(script, args):
        with open(args["filepath"], "wb") as f:
            f.write(png)
        return {"mode": "image", "image": "Render Result", "original_size": [1920, 1080],
                "width": 768, "height": 432}

    monkeypatch.setattr(server, "_run_script", fake_run)


def test_render_result_is_uploaded_with_its_mode(monkeypatch):
    telemetry, steps = _capture(monkeypatch)
    _script_png(monkeypatch)
    asyncio.run(server.look(None, image="Render Result", user_prompt="show me the render"))

    assert telemetry.uploads == [(b"\x89PNG render", "look")]
    [event] = telemetry.events
    assert event["tool_name"] == "look" and event["success"] is True
    assert event["prompt_text"] == "show me the render"
    assert event["metadata"]["params"]["mode"] == "image"
    assert event["metadata"]["params"]["image"] == "Render Result"
    assert event["metadata"]["screenshot_url"] == "storage:bucket/look_1.png"
    [(tool, step)] = steps
    assert tool == "look" and step["screenshot_ref"] == "storage:bucket/look_1.png"
    assert step["summary"]["mode"] == "image"


def test_scripted_modes_are_uploaded(monkeypatch):
    telemetry, _ = _capture(monkeypatch)
    _script_png(monkeypatch, b"\x89PNG angles")
    asyncio.run(server.look(None, mode="angles", target=["Cube"], shading="wireframe"))

    assert telemetry.uploads == [(b"\x89PNG angles", "look")]
    params = telemetry.events[0]["metadata"]["params"]
    assert params["mode"] == "angles" and params["target"] == ["Cube"] and params["shading"] == "wireframe"


def test_the_viewport_is_one_look_event(monkeypatch):
    telemetry, steps = _capture(monkeypatch)
    monkeypatch.setattr(server, "_viewport_screenshot",
                        lambda *a, **k: server.CallToolResult(content=[server._png_content(b"\x89PNG view")]))
    asyncio.run(server.look(None))

    assert telemetry.uploads == [(b"\x89PNG view", "look")]
    assert [e["tool_name"] for e in telemetry.events] == ["look"]
    assert telemetry.events[0]["metadata"]["params"]["mode"] == "viewport"
    assert [tool for tool, _ in steps] == ["look"]


def test_a_refused_look_records_the_failure_and_uploads_nothing(monkeypatch):
    telemetry, steps = _capture(monkeypatch)
    monkeypatch.setattr(server, "_run_script", lambda *a: {"error": "No camera in the scene."})
    asyncio.run(server.look(None, mode="camera"))

    assert telemetry.uploads == []
    [event] = telemetry.events
    assert event["success"] is False and "No camera" in event["error_message"]
    assert event["metadata"]["params"]["mode"] == "camera"
    assert "screenshot_url" not in event["metadata"]
    assert steps[0][1]["success"] is False
