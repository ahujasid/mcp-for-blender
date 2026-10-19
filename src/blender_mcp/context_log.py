"""
Opt-in local log of how much context the server's tool definitions and results take.

Set BLENDER_MCP_CONTEXT_LOG=1 in the MCP client's env for this server. Each
tools/list and tools/call appends one JSON line to context-log.jsonl in the
BlenderMCP data directory. Only names and sizes are written, never arguments,
code, prompts or result content, and the file is never uploaded anywhere.
"""

import base64
import json
import logging
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolRequest, CallToolResult, ImageContent, ListToolsRequest, TextContent

CONTEXT_LOG_ENV = "BLENDER_MCP_CONTEXT_LOG"
LOG_FILENAME = "context-log.jsonl"

logger = logging.getLogger("BlenderMCPServer")


def context_log_enabled() -> bool:
    return os.environ.get(CONTEXT_LOG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def context_log_path() -> Path:
    """Same directory as the telemetry UUID, so users have one place to look."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "BlenderMCP" / LOG_FILENAME


def _write(entry: dict) -> None:
    try:
        path = context_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug(f"Context log write failed: {e}")


def _json_chars(value: Any) -> int:
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return len(json.dumps(value, ensure_ascii=False))


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def _image_entry(block: ImageContent) -> dict:
    entry: dict = {"mime": block.mimeType, "base64_chars": len(block.data)}
    try:
        raw = base64.b64decode(block.data)
        size = _png_size(raw) or _jpeg_size(raw)
        if size:
            w, h = size
            # Anthropic's published estimate; other providers differ but scale similarly.
            entry.update(width=w, height=h, est_tokens=round(w * h / 750))
    except Exception:
        pass
    return entry


def measure_result(result: CallToolResult) -> dict:
    """Size a tools/call result as the client receives it."""
    text_chars = 0
    other_chars = 0
    images = []
    for block in result.content:
        if isinstance(block, TextContent):
            text_chars += len(block.text)
        elif isinstance(block, ImageContent):
            images.append(_image_entry(block))
        else:
            other_chars += _json_chars(block)

    structured_chars = _json_chars(result.structuredContent) if result.structuredContent else 0
    est = (text_chars + other_chars + structured_chars) // 4
    est += sum(img.get("est_tokens", 0) for img in images)
    entry = {"text_chars": text_chars, "est_tokens": est}
    if structured_chars:
        entry["structured_chars"] = structured_chars
    if other_chars:
        entry["other_chars"] = other_chars
    if result.isError:
        entry["is_error"] = True
    if images:
        entry["images"] = images
    return entry


def install(mcp, instructions: str) -> None:
    """Wrap the registered tools/list and tools/call handlers with size logging.

    Wraps whatever is registered at call time, so per-client tool filtering
    stays in place and the log sees exactly what the client receives.
    """
    if not context_log_enabled():
        return

    handlers = mcp._mcp_server.request_handlers
    list_handler, call_handler = handlers[ListToolsRequest], handlers[CallToolRequest]

    async def logged_list(request: ListToolsRequest):
        response = await list_handler(request)
        try:
            tools = response.root.tools
            sizes = {t.name: _json_chars(t) for t in tools}
            total = sum(sizes.values())
            _write({
                "event": "tools/list",
                "tool_count": len(tools),
                "total_chars": total,
                "est_tokens": total // 4,
                "instructions_chars": len(instructions),
                "per_tool_chars": dict(sorted(sizes.items(), key=lambda kv: -kv[1])),
            })
        except Exception as e:
            logger.debug(f"Context log could not measure tools/list: {e}")
        return response

    async def logged_call(request: CallToolRequest):
        start = time.time()
        response = await call_handler(request)
        entry = {"event": "tools/call", "tool": request.params.name,
                 "duration_ms": round((time.time() - start) * 1000)}
        try:
            entry.update(measure_result(response.root))
        except Exception as e:
            logger.debug(f"Context log could not measure {request.params.name}: {e}")
        _write(entry)
        return response

    handlers[ListToolsRequest] = logged_list
    handlers[CallToolRequest] = logged_call
    logger.info(f"Context log on: writing tool sizes to {context_log_path()} (local only)")
