"""Exercise the MCP metadata and call shapes that Codex consumes."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import ToolAnnotations

from blender_mcp import server, telemetry_decorator


def test_instructions_describe_the_blender_workflow():
    instructions = server.mcp.instructions
    assert instructions
    for guidance in (
        "Blender add-on is enabled",
        "Before making changes",
        "inspect the scene with get_scene_info",
        "Verify changes with get_scene_info or look",
        "execute_blender_code runs arbitrary Python",
        "untrusted input",
    ):
        assert guidance in instructions


def test_registered_tools_advertise_their_side_effects():
    local_read = (True, False, True, False)
    external_write = (False, False, False, True)
    destructive_write = (False, True, False, True)
    expected = {
        "get_addon_status": local_read,
        "get_scene_info": local_read,
        "look": local_read,
        "search_mentions": local_read,
        "open_viewport": local_read,
        "viewport_latest": local_read,
        "viewport_pick": local_read,
        "viewport_capture": (False, False, False, False),
        "disable_telemetry": (False, False, True, False),
        "search_assets": (True, False, True, True),
        "import_asset": destructive_write,
        "generate_3d": external_write,
        "generate_gaussian_world": external_write,
        "record_trajectory_feedback": external_write,
        "execute_blender_code": destructive_write,
    }
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools.keys() == expected.keys()
    for name, hints in expected.items():
        annotations = tools[name].annotations
        assert isinstance(annotations, ToolAnnotations), name
        assert (
            annotations.readOnlyHint,
            annotations.destructiveHint,
            annotations.idempotentHint,
            annotations.openWorldHint,
        ) == hints, name


def test_scene_info_schema_has_no_required_fields():
    tools = asyncio.run(server.mcp.list_tools())
    scene_info = next(tool for tool in tools if tool.name == "get_scene_info")
    assert not scene_info.inputSchema.get("required")


@pytest.mark.parametrize(
    ("arguments", "prompt"),
    [({}, ""), ({"user_prompt": None}, None), ({"user_prompt": "Inspect"}, "Inspect")],
)
def test_scene_info_keeps_recording_telemetry_with_optional_prompt(
    monkeypatch, arguments, prompt
):
    header = {
        "scene": "Test scene",
        "object_counts": {},
        "selected": [],
        "active": None,
        "mode": "OBJECT",
    }
    run_script = Mock(
        return_value={"header": header, "shown": 0, "total": 0, "lines": []}
    )
    telemetry = Mock()
    observe = Mock()
    monkeypatch.setattr(server, "_run_script", run_script)
    monkeypatch.setattr(telemetry_decorator, "get_telemetry", lambda: telemetry)
    monkeypatch.setattr(telemetry_decorator, "_record_observe_step", observe)
    monkeypatch.setattr(
        telemetry_decorator, "_consent_note", AsyncMock(return_value="")
    )

    _, result = asyncio.run(server.mcp.call_tool("get_scene_info", arguments))

    assert result["result"].startswith("Scene 'Test scene'")
    run_script.assert_called_once()
    observe.assert_called_once()
    assert observe.call_args.kwargs["goal_text"] == prompt
    assert observe.call_args.kwargs["success"] is True
    telemetry.record_event.assert_called_once()
    event = telemetry.record_event.call_args.kwargs
    assert event["tool_name"] == "get_scene_info"
    assert event["prompt_text"] == prompt
    assert event["success"] is True
