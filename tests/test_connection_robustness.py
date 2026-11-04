"""Connection robustness with many MCP sessions sharing one Blender.

Each MCP client (every Claude/Cursor session) runs its own server process with
its own persistent socket to the addon. These cover the ways that setup failed
intermittently: dead sockets after a Blender restart, connections dropped on
ordinary script errors, the main thread blocked on socket writes or frozen by
a burst of queued commands, and two Blenders silently sharing a port on
Windows.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time

import pytest

from blender_mcp import server as mcp_server
from test_server_threading import BlenderMCPServer, _free_port


class _Addon:
    """The addon's socket server with a thread playing Blender's main loop."""

    def __init__(self, port, execute=None):
        self.server = BlenderMCPServer(port=port)
        self.server.execute_command = execute or (
            lambda command: {"status": "success", "result": {"echo": command.get("params", {}).get("n")}}
        )
        self._stop = threading.Event()
        self.server.start()
        self._pump = threading.Thread(target=self._loop, daemon=True)
        self._pump.start()

    def _loop(self):
        while not self._stop.is_set():
            self.server._drain_command_queue()
            time.sleep(0.01)

    def stop(self):
        self._stop.set()
        self._pump.join(2.0)
        self.server.stop()


@pytest.fixture
def no_viewport_tracking(monkeypatch):
    monkeypatch.setattr(mcp_server.viewport_store, "command_started", lambda: None)
    monkeypatch.setattr(mcp_server.viewport_store, "command_finished", lambda kind: None)


def test_first_command_after_blender_restart_succeeds(no_viewport_tracking):
    """Every open session used to fail its next call after a Blender restart."""
    port = _free_port()
    addon = _Addon(port)
    conn = mcp_server.BlenderConnection("localhost", port)
    try:
        assert conn.send_command("ping", {"n": 1}) == {"echo": 1}
        addon.stop()
        time.sleep(0.2)
        addon = _Addon(port)
        assert conn.send_command("ping", {"n": 2}) == {"echo": 2}
    finally:
        conn.disconnect()
        addon.stop()


def test_blender_error_keeps_connection(no_viewport_tracking):
    """A failing script is not a broken connection; keep the socket."""
    port = _free_port()

    def execute(command):
        if command["type"] == "bad":
            return {"status": "error", "message": "NameError: name 'cube' is not defined"}
        return {"status": "success", "result": {"ok": True}}

    addon = _Addon(port, execute)
    conn = mcp_server.BlenderConnection("localhost", port)
    try:
        conn.send_command("ping")
        sock = conn.sock
        with pytest.raises(mcp_server.BlenderCommandError, match="NameError"):
            conn.send_command("bad")
        assert conn.sock is sock
        assert conn.send_command("ping") == {"ok": True}
    finally:
        conn.disconnect()
        addon.stop()


def test_large_response_is_received_quickly(no_viewport_tracking):
    """Receiving used to re-parse the whole buffer per chunk (quadratic)."""
    port = _free_port()
    blob = "A" * (16 * 1024 * 1024)
    addon = _Addon(port, lambda command: {"status": "success", "result": {"blob": blob}})
    conn = mcp_server.BlenderConnection("localhost", port)
    try:
        start = time.time()
        result = conn.send_command("big")
        assert len(result["blob"]) == len(blob)
        assert time.time() - start < 10
    finally:
        conn.disconnect()
        addon.stop()


def test_back_to_back_commands_on_one_connection_are_both_answered():
    """json.loads() rejected two concatenated commands, hanging the socket."""
    port = _free_port()
    addon = _Addon(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                json.dumps({"type": "a", "params": {"n": 1}}).encode()
                + json.dumps({"type": "b", "params": {"n": 2}}).encode()
            )
            decoder = json.JSONDecoder()
            buffer = ""
            replies = []
            deadline = time.time() + 5
            while len(replies) < 2 and time.time() < deadline:
                buffer += client.recv(8192).decode()
                while buffer:
                    try:
                        obj, end = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        break
                    replies.append(obj)
                    buffer = buffer[end:]
        assert [r["result"]["echo"] for r in replies] == [1, 2]
    finally:
        addon.stop()


def test_command_from_disconnected_client_is_skipped():
    """A client that gave up must not have its command run later."""
    port = _free_port()
    ran = []
    server = BlenderMCPServer(port=port)
    server.execute_command = lambda command: ran.append(command["type"]) or {"status": "success", "result": {}}
    server.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(json.dumps({"type": "abandoned"}).encode())
        deadline = time.time() + 2
        while server.command_queue.empty() and time.time() < deadline:
            time.sleep(0.01)
        client.close()
        # Let the handler notice the disconnect before the main thread runs.
        time.sleep(0.6)
        server._drain_command_queue()
        assert ran == []
    finally:
        server.stop()


def test_drain_yields_to_blender_ui_between_slow_commands():
    """A burst of queued commands must not freeze Blender for their total."""
    server = BlenderMCPServer(port=0)
    server.running = True
    ran = []

    def execute(command):
        time.sleep(0.06)
        ran.append(command["type"])
        return {"status": "success", "result": {}}

    server.execute_command = execute
    import queue

    for i in range(5):
        server.command_queue.put(({"type": f"c{i}"}, queue.Queue(maxsize=1)))
    next_interval = server._drain_command_queue()
    assert ran == ["c0"]
    assert next_interval == 0.0, "should ask to run again right after the UI updates"
    while server._drain_command_queue() == 0.0:
        pass
    assert ran == [f"c{i}" for i in range(5)]


@pytest.mark.skipif(sys.platform != "win32", reason="SO_REUSEADDR port sharing is Windows-specific")
def test_second_blender_cannot_share_the_port():
    """With SO_REUSEADDR, Windows let two listeners bind one port."""
    port = _free_port()
    first = BlenderMCPServer(port=port)
    first.execute_command = lambda command: {"status": "success", "result": {}}
    first.start()
    second = BlenderMCPServer(port=port)
    try:
        second.start()
        assert first.running
        assert not second.running
    finally:
        second.stop()
        first.stop()


def test_startup_does_not_wait_for_a_busy_blender(monkeypatch):
    """A busy Blender used to hold the initialize reply past the client's
    startup timeout, so the server showed as failed for the whole session."""
    import asyncio

    port = _free_port()
    busy = BlenderMCPServer(port=port)  # accepts connections, never drains
    busy.execute_command = lambda command: {"status": "success", "result": {}}
    busy.start()
    monkeypatch.setattr(mcp_server, "CLI_PORT", port)
    monkeypatch.setattr(mcp_server, "_blender_connection", None)
    monkeypatch.setattr(mcp_server, "_addon_handshake_checked", False)
    monkeypatch.setattr(mcp_server, "record_startup", lambda: None)

    async def enter_lifespan():
        start = time.time()
        async with mcp_server.server_lifespan(mcp_server.mcp):
            return time.time() - start

    try:
        assert asyncio.run(enter_lifespan()) < 2
    finally:
        busy.stop()
