"""Codex compatibility for generated MCP tool definitions."""

import asyncio

from blender_mcp.server import TOOL_ANNOTATIONS, mcp


def _listed_tools():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_every_registered_tool_has_explicit_annotations():
    tools = _listed_tools()

    assert set(TOOL_ANNOTATIONS) <= set(tools)
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is not None, name
        assert tool.annotations.openWorldHint is not None, name


def test_scene_inspection_does_not_require_telemetry_context():
    scene_info = _listed_tools()["get_scene_info"]
    required = scene_info.inputSchema.get("required", [])

    assert "user_prompt" not in required
    assert scene_info.inputSchema["properties"]["user_prompt"]["default"] == ""


def test_read_only_tools_are_marked_as_observation():
    tools = _listed_tools()

    for name in ("get_scene_info", "get_addon_status", "look", "search_mentions", "viewport_latest", "viewport_pick"):
        assert tools[name].annotations.readOnlyHint is True

    assert tools["search_assets"].annotations.readOnlyHint is True
    assert tools["search_assets"].annotations.openWorldHint is True


def test_mutations_and_external_actions_are_not_marked_read_only():
    tools = _listed_tools()

    code = tools["execute_blender_code"].annotations
    assert code.readOnlyHint is False
    assert code.destructiveHint is True
    assert code.openWorldHint is True

    texture = tools["import_asset"].annotations
    assert texture.readOnlyHint is False
    assert texture.destructiveHint is True

    download = tools["generate_3d"].annotations
    assert download.readOnlyHint is False
    assert download.destructiveHint is False
    assert download.openWorldHint is True

    telemetry = tools["disable_telemetry"].annotations
    assert telemetry.readOnlyHint is False
    assert telemetry.idempotentHint is True
    assert telemetry.openWorldHint is False
