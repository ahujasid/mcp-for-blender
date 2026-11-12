"""Tests for the opt-in local context-size log."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ImageContent, ListToolsRequest, TextContent

from blender_mcp import context_log


def _png(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "context-log.jsonl"
    monkeypatch.setattr(context_log, "context_log_path", lambda: path)
    return path


def _server():
    mcp = FastMCP("test")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo text back."""
        return text

    @mcp.tool()
    def snap() -> CallToolResult:
        """Return an image."""
        data = base64.b64encode(_png(750, 400)).decode()
        return CallToolResult(content=[ImageContent(type="image", data=data, mimeType="image/png"),
                                       TextContent(type="text", text="caption")])

    return mcp


def _run(mcp, request):
    handler = mcp._mcp_server.request_handlers[type(request)]
    return asyncio.run(handler(request))


def _entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_disabled_by_default(log_path, monkeypatch):
    monkeypatch.delenv(context_log.CONTEXT_LOG_ENV, raising=False)
    mcp = _server()
    context_log.install(mcp, "instructions")
    _run(mcp, ListToolsRequest(method="tools/list"))
    assert not log_path.exists()


def test_logs_sizes_not_content(log_path, monkeypatch):
    monkeypatch.setenv(context_log.CONTEXT_LOG_ENV, "1")
    mcp = _server()
    context_log.install(mcp, "x" * 40)

    _run(mcp, ListToolsRequest(method="tools/list"))
    secret = "do-not-log-me " * 10
    _run(mcp, CallToolRequest(method="tools/call", params=CallToolRequestParams(name="echo", arguments={"text": secret})))
    _run(mcp, CallToolRequest(method="tools/call", params=CallToolRequestParams(name="snap", arguments={})))

    listed, echoed, snapped = _entries(log_path)
    assert listed["event"] == "tools/list"
    assert listed["tool_count"] == 2
    assert listed["instructions_chars"] == 40
    assert set(listed["per_tool_chars"]) == {"echo", "snap"}

    assert echoed["tool"] == "echo"
    assert echoed["text_chars"] == len(secret)
    assert "do-not-log-me" not in log_path.read_text()

    assert snapped["images"][0]["width"] == 750
    assert snapped["images"][0]["height"] == 400
    assert snapped["images"][0]["est_tokens"] == 400
    assert snapped["est_tokens"] == 400 + len("caption") // 4


def test_keeps_existing_tools_list_filter(log_path, monkeypatch):
    monkeypatch.setenv(context_log.CONTEXT_LOG_ENV, "1")
    mcp = _server()

    async def only_echo():
        return [t for t in await mcp.list_tools() if t.name == "echo"]

    mcp._mcp_server.list_tools()(only_echo)
    context_log.install(mcp, "")
    response = _run(mcp, ListToolsRequest(method="tools/list"))

    assert [t.name for t in response.root.tools] == ["echo"]
    assert list(_entries(log_path)[0]["per_tool_chars"]) == ["echo"]
