"""Regression coverage for split multi-byte UTF-8 sequences in the socket buffer.

The bug: `_handle_client` accumulates `recv()` chunks into `buffer` and does
`buffer.decode('utf-8')` before attempting `json.loads()`. Only
`json.JSONDecodeError` was caught, treated as "incomplete data, wait for more".
If a multi-byte UTF-8 character (e.g. an accented letter, CJK text, or an
emoji in an object name or in LLM-generated code) is split across a `recv()`
chunk boundary, `.decode('utf-8')` raises `UnicodeDecodeError` instead -
uncaught here, so it falls through to the outer `except Exception`, which
logs and `break`s. The command is dropped and the connection is torn down,
even though the rest of the payload was already sitting in the OS receive
buffer waiting to be read.

A real loopback socket won't reliably reproduce an exact byte-offset split
(the OS may coalesce separate `sendall()` calls into one `recv()`), so this
drives `_handle_client` directly with a fake socket that returns pre-scripted
chunks - deterministic, no network, no flakiness.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from test_server_threading import BlenderMCPServer


class _ScriptedSocket:
    """Fake client socket returning pre-scripted recv() chunks, one per call."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []

    def settimeout(self, timeout):
        pass

    def recv(self, bufsize, flags=0):
        if flags & socket.MSG_PEEK:
            return self._chunks[0][:1] if self._chunks else b""
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


def _make_server():
    server = BlenderMCPServer(port=0)
    server.execute_command = lambda command: {"status": "success", "result": {}}
    return server


def _split_after_lead_byte(payload: bytes) -> int:
    """Index right after a multi-byte UTF-8 lead byte's first byte.

    Splitting there guarantees the first chunk ends mid-character, so
    decoding it alone as UTF-8 raises UnicodeDecodeError.
    """
    for i, b in enumerate(payload):
        if b >= 0xC0:  # lead byte of a 2/3/4-byte sequence
            return i + 1
    raise AssertionError("payload has no multi-byte UTF-8 character to split")


def test_split_multibyte_utf8_boundary_is_not_dropped():
    payload = json.dumps(
        {"type": "ping", "params": {"note": "café ☕ 日本語"}}, ensure_ascii=False
    ).encode("utf-8")
    split_idx = _split_after_lead_byte(payload)
    chunk1, chunk2 = payload[:split_idx], payload[split_idx:]

    # Sanity check: confirm the split really does land mid-character, i.e.
    # this fixture actually exercises the bug and isn't accidentally valid.
    with pytest.raises(UnicodeDecodeError):
        chunk1.decode("utf-8")

    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([chunk1, chunk2]))

    assert not server.command_queue.empty(), (
        "command was dropped: a multi-byte UTF-8 character split across a "
        "recv() chunk boundary killed the connection instead of waiting for "
        "the rest of the buffer"
    )
    command, _client = server.command_queue.get_nowait()
    assert command["type"] == "ping"
    assert command["params"]["note"] == "café ☕ 日本語"


def test_split_multibyte_utf8_boundary_keeps_handler_loop_alive():
    """A second command sent right after the split payload must still arrive.

    If the split killed the loop, this second command would never be queued.
    """
    first = json.dumps(
        {"type": "ping", "params": {"note": "emoji test 🎨"}}, ensure_ascii=False
    ).encode("utf-8")
    split_idx = _split_after_lead_byte(first)
    second = json.dumps({"type": "ping", "params": {}}).encode("utf-8")

    server = _make_server()
    queued = []
    server.execute_command = lambda command: (
        queued.append(command) or {"status": "success", "result": {}}
    )
    server.running = True
    client = _ScriptedSocket([first[:split_idx], first[split_idx:], second])
    handler = threading.Thread(target=server._handle_client, args=(client,))
    handler.start()

    # Play Blender's main loop: each command waits for its reply before the
    # handler reads the next one off the connection.
    deadline = time.time() + 5.0
    while handler.is_alive() and time.time() < deadline:
        server._drain_command_queue()
        time.sleep(0.01)
    server.running = False
    handler.join(2.0)

    assert len(queued) == 2, f"expected both commands queued, got {queued}"
