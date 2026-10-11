"""Length-prefixed framing over the addon bridge, and its legacy fallback.

The wire format is [4-byte big-endian length][UTF-8 JSON]. It is opt-in: the
server only sends frames after the addon's get_addon_info handshake advertises
the capability, and the addon answers in the format the request arrived in.
That keeps every combination working:

    new server + new addon    frames (exact message boundaries, #279)
    new server + old addon    raw JSON, exactly as before
    old server + new addon    raw JSON in, raw JSON out (#311)
    new addon alone           raw JSON until a framing-capable server connects

The add-on cannot be imported without Blender, so BlenderMCPServer comes from
test_server_threading's AST loader.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from conftest import ROOT_ADDON
from test_server_threading import BlenderMCPServer, _free_port, _pump
from test_socket_unicode import _ScriptedSocket
from test_polypizza import _load_addon, _scene

SERVER = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "server.py"
BUNDLED = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon.py"


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _recv_exact(sock, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def _recv_frame(sock) -> bytes:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    return _recv_exact(sock, length)


def _make_server():
    server = BlenderMCPServer(port=_free_port())
    server.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    return server


def _queue_all(server):
    items = []
    while not server.command_queue.empty():
        items.append(server.command_queue.get_nowait())
    return items


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------- add-on: the parser


def test_framed_message_split_across_recv_chunks_is_reassembled():
    payload = _frame(json.dumps({"type": "ping"}).encode())
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([payload[:3], payload[3:]]))

    command, _client, framed = server.command_queue.get_nowait()
    assert command["type"] == "ping"
    assert framed is True


def test_multiple_frames_in_one_recv_are_all_served():
    payload = b"".join(
        _frame(json.dumps({"type": "ping", "params": {"n": n}}).encode())
        for n in range(3)
    )
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([payload]))

    commands = [(c["params"]["n"], framed) for c, _client, framed in _queue_all(server)]
    assert commands == [(0, True), (1, True), (2, True)]


def test_frames_over_16_mib_are_still_recognized_as_frames():
    # A length in [16 MiB, 64 MiB] no longer starts with 0x00, so a
    # first-byte-only check would misread it as raw JSON and stall. Trailing
    # whitespace keeps the payload valid JSON without building a huge object.
    payload = json.dumps({"type": "ping"}).encode() + b" " * (16 * 1024 * 1024)
    frame = _frame(payload)
    assert frame[0] != 0

    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([frame[:1000], frame[1000:]]))

    command, _client, framed = server.command_queue.get_nowait()
    assert command["type"] == "ping"
    assert framed is True


def test_coalesced_raw_commands_are_both_served():
    # The old parser did json.loads(buffer) once, so two commands delivered in
    # the same recv() failed with "Extra data" and jammed until the timeout.
    payload = (
        json.dumps({"type": "ping"}).encode()
        + json.dumps({"type": "ping"}).encode()
    )
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([payload]))

    commands = [(c["type"], framed) for c, _client, framed in _queue_all(server)]
    assert commands == [("ping", False), ("ping", False)]


def test_raw_then_framed_on_one_connection_is_served_as_sent():
    # What a new server does: raw get_addon_info first, then framed commands.
    payload = json.dumps({"type": "get_addon_info"}).encode()
    payload += _frame(json.dumps({"type": "ping"}).encode())
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([payload]))

    commands = [(c["type"], framed) for c, _client, framed in _queue_all(server)]
    assert commands == [("get_addon_info", False), ("ping", True)]


def test_oversized_frame_length_is_rejected_without_queueing():
    # A raw client's JSON starts with '{' (0x7b), never 0x00, so a giant
    # length here is genuinely malformed. It must not be waited on forever.
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([struct.pack(">I", 2 ** 30)]))

    assert _queue_all(server) == []


def test_malformed_frame_payload_is_rejected_without_queueing():
    server = _make_server()
    server.running = True
    server._handle_client(_ScriptedSocket([_frame(b"not json")]))

    assert _queue_all(server) == []


# ------------------------------------------------- add-on: replies over a socket


def test_addon_answers_a_framed_client_with_a_frame():
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(_frame(json.dumps({"type": "ping"}).encode()))
            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()
            client.settimeout(5)
            payload = _recv_frame(client)

        assert json.loads(payload.decode())["result"]["echo"] == "ping"
    finally:
        server.stop()


def test_legacy_raw_client_is_answered_with_raw_json():
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode())
            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()
            client.settimeout(5)
            # json.loads would fail on a length prefix, so this also proves no
            # frame was sent - the #311 regression.
            response = json.loads(client.recv(8192).decode())

        assert response["result"]["echo"] == "ping"
    finally:
        server.stop()


# ---------------------------------------------------- server: framing send/recv


class _FakeAddon:
    """One-shot fake addon: reads one command, sends one reply, both formats."""

    def __init__(self, reply, framed, oversize_header=False):
        self._reply = reply
        self._framed = framed
        self._oversize_header = oversize_header
        self.received = []
        self.sock = socket.socket()
        self.sock.bind(("localhost", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            try:
                if self._framed:
                    payload = _recv_frame(conn)
                else:
                    payload = conn.recv(8192)
                if not payload:
                    return
                self.received.append(json.loads(payload.decode()))
                if self._oversize_header:
                    conn.sendall(struct.pack(">I", 2 ** 30))
                    return
                reply = json.dumps(self._reply).encode()
                conn.sendall(_frame(reply) if self._framed else reply)
            except OSError:
                return

    def close(self):
        self.sock.close()


def test_connection_sends_and_reads_frames_when_enabled():
    from blender_mcp.server import BlenderConnection

    addon = _FakeAddon({"status": "success", "result": {"pong": True}}, framed=True)
    conn = BlenderConnection(host="localhost", port=addon.port)
    try:
        assert conn.connect()
        conn.framed = True
        assert conn.send_command("ping") == {"pong": True}
        assert addon.received == [{"type": "ping", "params": {}}]
    finally:
        conn.disconnect()
        addon.close()


def test_connection_stays_raw_against_a_legacy_addon():
    from blender_mcp.server import BlenderConnection

    addon = _FakeAddon({"status": "success", "result": {"pong": True}}, framed=False)
    conn = BlenderConnection(host="localhost", port=addon.port)
    try:
        assert conn.connect()
        assert conn.framed is False
        assert conn.send_command("ping") == {"pong": True}
        assert addon.received == [{"type": "ping", "params": {}}]
    finally:
        conn.disconnect()
        addon.close()


def test_a_reconnected_socket_starts_on_raw_again():
    # After a dropped socket the next command reconnects; framing must not be
    # reused until a fresh handshake on the new socket says it is supported.
    from blender_mcp.server import BlenderConnection

    addon = _FakeAddon({"status": "success", "result": {"pong": True}}, framed=False)
    conn = BlenderConnection(host="localhost", port=addon.port)
    try:
        assert conn.connect()
        conn.framed = True
        conn.sock = None  # the drop that _send_command_locked performs
        assert conn.connect()
        assert conn.framed is False
    finally:
        conn.disconnect()
        addon.close()


def test_oversized_response_frame_is_an_error_not_a_hang():
    from blender_mcp.server import BlenderConnection

    addon = _FakeAddon({"status": "success"}, framed=True, oversize_header=True)
    conn = BlenderConnection(host="localhost", port=addon.port)
    try:
        assert conn.connect()
        conn.framed = True
        with pytest.raises(Exception, match="Communication error"):
            conn.send_command("ping")
    finally:
        conn.disconnect()
        addon.close()


# ------------------------------------------------------------ server: handshake


def test_new_server_and_new_addon_upgrade_and_round_trip(monkeypatch):
    # End to end over real sockets: the handshake goes raw, the capability
    # flips the connection to frames, and the next command round-trips framed.
    from blender_mcp import server as server_module
    from blender_mcp.server import BlenderConnection

    server = _make_server()

    def execute(command):
        if command.get("type") == "get_addon_info":
            return {
                "status": "success",
                "result": {
                    "protocol_version": 13,
                    "addon_version": [1, 8],
                    "capabilities": [server_module.FRAMING_CAPABILITY],
                    "blender_version": "4.2.0",
                    "premium_generators": [],
                },
            }
        return {"status": "success", "result": {"echo": command.get("type")}}

    server.execute_command = execute
    server.start()
    conn = BlenderConnection(host="localhost", port=server.port)
    try:
        assert conn.connect()
        pump = threading.Thread(target=_pump, args=(server,), daemon=True)
        pump.start()

        monkeypatch.setattr(server_module, "_addon_handshake", None)
        monkeypatch.setattr(server_module, "_addon_handshake_checked", False)
        server_module._maybe_handshake_addon(conn)
        assert conn.framed is True

        assert conn.send_command("ping") == {"echo": "ping"}
    finally:
        conn.disconnect()
        server.stop()


class _HandshakeConnection:
    """Records send_command calls; returns a canned get_addon_info payload."""

    def __init__(self, capabilities):
        self._capabilities = capabilities
        self.framed = False
        self.sent = []

    def send_command(self, command_type, params=None, read_only=False):
        self.sent.append(command_type)
        return {
            "protocol_version": 13,
            "addon_version": [1, 8],
            "capabilities": self._capabilities,
            "blender_version": "4.2.0",
            "premium_generators": [],
        }


def test_handshake_enables_framing_only_when_advertised(monkeypatch):
    from blender_mcp import server as server_module

    monkeypatch.setattr(server_module, "_addon_handshake", None)

    framed = _HandshakeConnection([server_module.FRAMING_CAPABILITY])
    monkeypatch.setattr(server_module, "_addon_handshake_checked", False)
    server_module._maybe_handshake_addon(framed)
    assert framed.framed is True
    assert framed.sent == ["get_addon_info"]

    legacy = _HandshakeConnection(["get_scene_info"])
    monkeypatch.setattr(server_module, "_addon_handshake_checked", False)
    server_module._maybe_handshake_addon(legacy)
    assert legacy.framed is False
    assert legacy.sent == ["get_addon_info"]


def test_current_addon_advertises_the_framing_capability(monkeypatch):
    addon = _load_addon(monkeypatch, _scene())
    info = addon.BlenderMCPServer.get_addon_info(None)
    assert addon.FRAMING_CAPABILITY in info["capabilities"]


# ------------------------------------------------------------ cross-file drift


def _wire_constants(path: Path):
    source = path.read_text(encoding="utf-8")
    capability = re.search(r'^FRAMING_CAPABILITY = "([^"]+)"', source, re.M)
    max_frame = re.search(r"^MAX_FRAME_BYTES = ([0-9][0-9 *]*)$", source, re.M)
    assert capability and max_frame, f"{path} is missing a framing constant"
    return capability.group(1), max_frame.group(1).replace(" ", "")


def test_wire_constants_stay_in_sync_across_files():
    assert _wire_constants(ROOT_ADDON) == _wire_constants(BUNDLED) == _wire_constants(SERVER)
