"""Regression coverage for split multi-byte UTF-8 in the client's socket reads.

Companion to `test_socket_unicode.py`, which covers the same class of bug on the
addon side (`_handle_client`). The client had the mirror-image defect in
`BlenderConnection.receive_full_response`: it joins the received chunks and runs
`data.decode('utf-8')` before `json.loads()`, and caught only
`json.JSONDecodeError`, treating anything else as a fatal receive error. A
multi-byte UTF-8 character (a CJK/emoji object name, an accented path - all
routine in a Blender scene) split across a `recv(8192)` chunk boundary raises
`UnicodeDecodeError` instead, which escaped to the outer `except Exception`,
logged "Error during receive", and re-raised - failing an MCP tool call even
though the rest of the response was already sitting in the OS receive buffer
waiting on the next `recv()`.

A real loopback socket won't reliably reproduce an exact byte-offset split
(the OS may coalesce separate `sendall()` calls into one `recv()`), so this
drives `receive_full_response` directly with a fake socket that returns
pre-scripted chunks - deterministic, no network, no flakiness.
"""

from __future__ import annotations

import ast
import json
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import pytest

SERVER_PY = Path(__file__).resolve().parent.parent / "src" / "blender_mcp" / "server.py"


class _ScriptedSocket:
    """Fake socket returning pre-scripted recv() chunks, one per call."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def settimeout(self, timeout):
        pass

    def recv(self, bufsize):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


def _load_connection_class():
    """Compile BlenderConnection out of server.py without importing `mcp`.

    server.py pulls in the whole MCP stack at module scope, which isn't
    installed in the test environment, so lift just the class out by AST and
    run it against the handful of names it actually references.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    body = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BlenderConnection"
    ]
    assert body, "BlenderConnection not found in server.py"

    import logging

    module = ast.Module(body=body, type_ignores=[])
    namespace = {
        "socket": socket,
        "json": json,
        "threading": threading,
        "dataclass": dataclass,
        "field": field,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "logger": logging.getLogger("test"),
    }
    exec(compile(ast.fix_missing_locations(module), str(SERVER_PY), "exec"), namespace)
    return namespace["BlenderConnection"]


def _split_after_lead_byte(payload: bytes) -> int:
    """Index right after a multi-byte UTF-8 lead byte's first byte.

    Splitting there guarantees the first chunk ends mid-character, so decoding
    it alone as UTF-8 raises UnicodeDecodeError.
    """
    for i, b in enumerate(payload):
        if b >= 0xC0:  # lead byte of a 2/3/4-byte sequence
            return i + 1
    raise AssertionError("payload has no multi-byte UTF-8 character to split")


def _response_with(name: str) -> bytes:
    return json.dumps(
        {"status": "success", "result": {"objects": [{"name": name}]}},
        ensure_ascii=False,
    ).encode("utf-8")


def test_receive_full_response_survives_split_multibyte_utf8():
    """A split character is incomplete data, not a receive failure."""
    payload = _response_with("カメラ café 🎨 日本語")
    split_idx = _split_after_lead_byte(payload)

    # Sanity check: confirm the split really does land mid-character, i.e.
    # this fixture actually exercises the bug and isn't accidentally valid.
    with pytest.raises(UnicodeDecodeError):
        payload[:split_idx].decode("utf-8")

    connection = _load_connection_class()(host="localhost", port=9876)
    data = connection.receive_full_response(
        _ScriptedSocket([payload[:split_idx], payload[split_idx:]])
    )

    assert data == payload
    assert json.loads(data.decode("utf-8"))["result"]["objects"][0]["name"] == (
        "カメラ café 🎨 日本語"
    )


def test_receive_full_response_returns_whole_buffer_across_many_chunks():
    """Reassembly stays correct when the split lands on an ASCII boundary too.

    Guards against "fix" that simply swallows the decode error without
    continuing to read: the returned bytes must be the full payload.
    """
    payload = _response_with("カメラ café 🎨 日本語")
    connection = _load_connection_class()(host="localhost", port=9876)
    chunks = [payload[i : i + 7] for i in range(0, len(payload), 7)]

    assert connection.receive_full_response(_ScriptedSocket(chunks)) == payload


def test_receive_full_response_still_raises_on_truly_incomplete_json():
    """The pre-existing contract is unchanged for a payload that never completes.

    Catching UnicodeDecodeError must not swallow the "Incomplete JSON response
    received" error raised once the socket goes quiet with a short buffer.
    """
    payload = _response_with("カメラ café 🎨 日本語")
    split_idx = _split_after_lead_byte(payload)
    connection = _load_connection_class()(host="localhost", port=9876)

    with pytest.raises(Exception, match="Incomplete JSON response received"):
        connection.receive_full_response(_ScriptedSocket([payload[:split_idx]]))
