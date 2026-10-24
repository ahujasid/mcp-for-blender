"""Tests for the addon's socket server threading model (no Blender required).

addon.py cannot be imported without bpy, so BlenderMCPServer is lifted out by
AST and executed against stubs.

The bug these cover: commands used to be dispatched by calling
bpy.app.timers.register() from a client thread. bpy.app.timers is main-thread
only, so on Windows the callback could be silently dropped - the connection was
accepted but no response ever arrived, and the client hung until its 180s
socket timeout.
"""

from __future__ import annotations

import ast
import json
import socket
import sys
import threading
import time
import types
from contextlib import contextmanager

from conftest import ROOT_ADDON


class _NullEditRecorder:
    """Stands in for the addon's UserEditRecorder; captures nothing."""

    def drain(self):
        return []

    @contextmanager
    def agent_command(self):
        yield


def _load_server_class(addon_path=ROOT_ADDON):
    """Compile BlenderMCPServer from addon.py against stub modules."""
    source = addon_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    body = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer"
    ]
    assert body, "BlenderMCPServer not found in addon.py"

    main_thread = threading.current_thread()
    registered = {}

    class _Timers:
        """Stub that enforces the real bpy.app.timers main-thread constraint."""

        def register(self, fn, first_interval=0.0, persistent=False):
            if threading.current_thread() is not main_thread:
                raise AssertionError(
                    "bpy.app.timers.register() called from a non-main thread"
                )
            registered[fn] = True

        def unregister(self, fn):
            registered.pop(fn, None)

        def is_registered(self, fn):
            return fn in registered

    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(background=False, timers=_Timers())
    bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace())

    namespace = {
        "bpy": bpy,
        "socket": socket,
        "threading": threading,
        "json": json,
        "time": time,
        "queue": __import__("queue"),
        "traceback": __import__("traceback"),
        "os": __import__("os"),
        "get_blendermcp_addon_preferences": lambda context=None: None,
        "RODIN_FREE_TRIAL_KEY": "vibecoding",
        # start()/stop() drive the edit-capture handlers, which live at module
        # scope in addon.py and so are not carried in by lifting the class.
        "_register_edit_capture_handlers": lambda: False,
        "_unregister_edit_capture_handlers": lambda: None,
        "get_edit_recorder": lambda: _NullEditRecorder(),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "<addon>", "exec"), namespace)
    return namespace["BlenderMCPServer"], registered


BlenderMCPServer, _registered = _load_server_class()


def _free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _make_server():
    server = BlenderMCPServer(port=_free_port())
    # Stub out command execution; these tests are about transport, not bpy.
    server.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    return server


def _pump(server, deadline=3.0):
    """Act as Blender's main loop, draining the queue until timeout."""
    end = time.time() + deadline
    while time.time() < end:
        server._drain_command_queue()
        time.sleep(0.01)


def test_client_thread_never_registers_a_timer():
    """The regression itself: dispatch must not touch bpy.app.timers off-thread.

    The _Timers stub raises if register() is called from a non-main thread, so
    the old per-command bpy.app.timers.register() would surface here.
    """
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())

        assert response["status"] == "success"
        assert response["result"]["echo"] == "ping"
    finally:
        server.stop()


def test_command_is_queued_not_executed_on_client_thread():
    """Without a main-loop pump, the command waits in the queue - never lost."""
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())

            # No pump running, so nothing should execute yet.
            deadline = time.time() + 2.0
            while time.time() < deadline and server.command_queue.empty():
                time.sleep(0.01)

            assert not server.command_queue.empty(), "command was dropped, not queued"

            # Now pump once: the queued command is serviced.
            server._drain_command_queue()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
            assert response["status"] == "success"
    finally:
        server.stop()


def test_stop_releases_client_threads():
    """stop() must unblock handlers so they cannot outlive a restart.

    Orphaned daemon threads parked in recv() were what produced the
    WinError 10054 after toggling the addon.
    """
    server = _make_server()
    server.start()

    client = socket.create_connection(("localhost", server.port), timeout=5)
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with server._clients_lock:
                if server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert server._clients, "server did not track the client socket"

        server.stop()

        # Handler threads should have exited and deregistered themselves.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with server._clients_lock:
                if not server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert not server._clients, "client sockets still tracked after stop()"

        assert not bpy_timer_registered(server), "drain timer left registered"
    finally:
        try:
            client.close()
        except OSError:
            pass


def bpy_timer_registered(server):
    return server._drain_command_queue in _registered


def test_restart_rebinds_port_cleanly():
    """A stopped server must fully release the port for the next start()."""
    port = _free_port()

    first = BlenderMCPServer(port=port)
    first.execute_command = lambda command: {"status": "success", "result": {}}
    first.start()
    with socket.create_connection(("localhost", port), timeout=5):
        pass
    first.stop()

    second = BlenderMCPServer(port=port)
    second.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    second.start()
    try:
        with socket.create_connection(("localhost", port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())
            pump = threading.Thread(target=_pump, args=(second,), daemon=True)
            pump.start()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
        assert response["status"] == "success"
    finally:
        second.stop()
