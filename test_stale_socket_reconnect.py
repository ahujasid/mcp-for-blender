"""Behavioural tests for recovering from a stale cached socket.

BlenderConnection keeps one socket open across commands. If Blender closes its
end while the client is idle, the cached handle is dead and the next command used
to fail with "Connection to Blender lost" (WinError 10054 on Windows) even though
the command never reached Blender. These tests drive a fake addon server over
loopback to cover that path without needing Blender.
"""
import json
import pathlib
import socket
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).with_name("src")))

from blender_mcp.server import BlenderConnection  # noqa: E402


class FakeAddonServer:
    """Minimal stand-in for the addon's socket server."""

    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("localhost", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self.connections = []
        self.commands = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            self.connections.append(client)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        buffer = b""
        while not self._stop.is_set():
            try:
                data = client.recv(8192)
            except OSError:
                return
            if not data:
                return
            buffer += data
            try:
                command = json.loads(buffer.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            buffer = b""
            self.commands.append(command)
            reply = {"status": "success", "result": {"echoed": command["type"]}}
            try:
                client.sendall(json.dumps(reply).encode("utf-8"))
            except OSError:
                return

    def wait_for_connections(self, count, timeout=5.0):
        """Block until the accept loop has picked up `count` connections."""
        deadline = time.monotonic() + timeout
        while len(self.connections) < count:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"expected {count} connection(s), saw {len(self.connections)}"
                )
            time.sleep(0.01)

    def drop_current_connection(self):
        """Close the server side of the live connection, as Blender does on idle."""
        self.wait_for_connections(len(self.connections) or 1)
        self.connections[-1].close()

    def close(self):
        self._stop.set()
        self._listener.close()


def test_peer_closed_is_false_while_idle():
    server = FakeAddonServer()
    conn = BlenderConnection(host="localhost", port=server.port)
    try:
        assert conn.connect()
        # An open connection with no pending data must not be mistaken for a dead one.
        assert conn._peer_closed() is False
    finally:
        conn.disconnect()
        server.close()


def test_peer_closed_detects_server_side_close():
    server = FakeAddonServer()
    conn = BlenderConnection(host="localhost", port=server.port)
    try:
        assert conn.connect()
        server.drop_current_connection()
        # The FIN has to land before the peek can see it.
        deadline = time.monotonic() + 5.0
        while not conn._peer_closed():
            assert time.monotonic() < deadline, "close was never observed"
            time.sleep(0.01)
    finally:
        conn.disconnect()
        server.close()


def test_command_after_idle_close_reconnects():
    """The regression: a command issued after the server dropped the connection."""
    server = FakeAddonServer()
    conn = BlenderConnection(host="localhost", port=server.port)
    try:
        assert conn.send_command("get_scene_info") == {"echoed": "get_scene_info"}

        server.drop_current_connection()

        # Previously raised "Connection to Blender lost"; must now just work.
        assert conn.send_command("execute_code") == {"echoed": "execute_code"}
        server.wait_for_connections(2)
        assert [c["type"] for c in server.commands] == ["get_scene_info", "execute_code"]
    finally:
        conn.disconnect()
        server.close()


def test_command_is_not_sent_twice_on_reconnect():
    """A reconnect must re-send the command once, never duplicate it."""
    server = FakeAddonServer()
    conn = BlenderConnection(host="localhost", port=server.port)
    try:
        conn.send_command("first")
        server.drop_current_connection()
        conn.send_command("mutating_command")
        assert [c["type"] for c in server.commands].count("mutating_command") == 1
    finally:
        conn.disconnect()
        server.close()
