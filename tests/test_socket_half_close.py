"""EOF ends requests, but queued responses still belong to the client.

Real sockets exercise half-close and write failure. Queue notifications and
thread joins force EOF to be handled before the main-thread drain runs; no
timer pump or sleeps can let a premature close accidentally win the test.
"""

import json
import queue
import socket
import threading
from contextlib import contextmanager

import pytest

from conftest import REPO_ROOT, ROOT_ADDON
from test_server_threading import _load_server_class


class _NotifyingQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.queued = threading.Semaphore(0)

    def put(self, item):
        super().put(item)
        self.queued.release()


@pytest.fixture(
    params=[ROOT_ADDON, REPO_ROOT / "src/blender_mcp/bundled/addon.py"],
    ids=["root", "bundled"],
)
def server(request):
    cls, _ = _load_server_class(request.param)
    instance = cls(port=0)
    instance.running = True
    instance.command_queue = _NotifyingQueue()
    instance.execute_command = lambda command: {
        "status": "success", "result": {"echo": command["type"]}
    }
    return instance


@contextmanager
def _connection(server, wrap=lambda accepted: accepted):
    peer, accepted = socket.socketpair()
    peer.settimeout(2)
    handler = threading.Thread(
        target=server._handle_client, args=(wrap(accepted),), daemon=True
    )
    handler.start()
    try:
        yield peer, handler
    finally:
        server.stop()
        peer.close()
        handler.join(timeout=2)
        assert not handler.is_alive(), "client handler outlived server shutdown"


def _queue_request(server, peer, command_type):
    peer.sendall(json.dumps({"type": command_type}).encode())
    assert server.command_queue.queued.acquire(timeout=2), "request was not queued"


def _finish_requests(peer, handler):
    peer.shutdown(socket.SHUT_WR)
    handler.join(timeout=2)
    assert not handler.is_alive(), "EOF left a client handler waiting"


def _read_to_eof(peer):
    chunks = []
    while data := peer.recv(8192):
        chunks.append(data)
    return b"".join(chunks)


def _read_responses(peer):
    remaining = _read_to_eof(peer).decode()
    responses = []
    decoder = json.JSONDecoder()
    while remaining.strip():
        response, end = decoder.raw_decode(remaining.lstrip())
        responses.append(response)
        remaining = remaining.lstrip()[end:]
    return responses


def _assert_no_clients(server):
    assert not server._clients
    assert not getattr(server, "_pending_responses", {})
    assert not getattr(server, "_read_closed_clients", set())


def test_half_close_receives_response_after_handler_exits(server):
    """Closing in the handler's finally block loses this queued response."""
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "ping")
        _finish_requests(peer, handler)

        server._drain_command_queue()

        assert _read_responses(peer) == [
            {"status": "success", "result": {"echo": "ping"}}
        ]
        _assert_no_clients(server)


def test_half_close_keeps_socket_until_all_queued_responses_are_sent(server):
    """Closing after the first pending response drops the second response."""
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "first")
        _queue_request(server, peer, "second")
        _finish_requests(peer, handler)

        server._drain_command_queue()

        assert _read_responses(peer) == [
            {"status": "success", "result": {"echo": "first"}},
            {"status": "success", "result": {"echo": "second"}},
        ]
        _assert_no_clients(server)


def test_half_close_receives_command_error(server):
    def fail(command):
        raise ValueError("command failed")

    server.execute_command = fail
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "fail")
        _finish_requests(peer, handler)

        server._drain_command_queue()

        assert _read_responses(peer) == [{"status": "error", "message": "command failed"}]
        _assert_no_clients(server)


def test_half_close_during_command_execution_keeps_inflight_response(server):
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "ping")

        def execute(command):
            # The queue is empty now, but its dequeued command still owns a
            # response. EOF must not close the socket during execution.
            _finish_requests(peer, handler)
            return {"status": "success", "result": {"echo": command["type"]}}

        server.execute_command = execute
        server._drain_command_queue()

        assert _read_responses(peer) == [
            {"status": "success", "result": {"echo": "ping"}}
        ]
        _assert_no_clients(server)


def test_response_sent_before_eof_keeps_connection_reusable(server):
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "first")
        server._drain_command_queue()
        assert json.loads(peer.recv(8192)) == {
            "status": "success", "result": {"echo": "first"}
        }

        _queue_request(server, peer, "second")
        _finish_requests(peer, handler)
        server._drain_command_queue()

        assert _read_responses(peer) == [
            {"status": "success", "result": {"echo": "second"}}
        ]
        _assert_no_clients(server)


@pytest.mark.parametrize("payload", [b"", b'{"type":'])
def test_eof_without_a_complete_request_closes_immediately(server, payload):
    with _connection(server) as (peer, handler):
        peer.sendall(payload)
        _finish_requests(peer, handler)

        assert _read_to_eof(peer) == b""
        assert server.command_queue.empty()
        _assert_no_clients(server)


def test_stop_closes_half_closed_client_with_pending_response(server):
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "ping")
        _finish_requests(peer, handler)

        server.stop()

        assert _read_to_eof(peer) == b""
        assert server.command_queue.empty()
        _assert_no_clients(server)


def test_disconnected_peer_does_not_leak_socket_after_send_failure(server):
    with _connection(server) as (peer, handler):
        _queue_request(server, peer, "ping")
        _finish_requests(peer, handler)
        peer.close()

        server._drain_command_queue()

        assert server.command_queue.empty()
        _assert_no_clients(server)


def test_receive_error_closes_without_waiting_for_pending_response(server):
    class ReceiveErrorSocket:
        def __init__(self, accepted):
            self.accepted = accepted
            self.received = False

        def __getattr__(self, name):
            return getattr(self.accepted, name)

        def recv(self, size):
            if self.received:
                raise ConnectionResetError("peer reset")
            self.received = True
            return self.accepted.recv(size)

    with _connection(server, wrap=ReceiveErrorSocket) as (peer, handler):
        _queue_request(server, peer, "ping")
        handler.join(timeout=2)
        assert not handler.is_alive()
        assert _read_to_eof(peer) == b""
        _assert_no_clients(server)

        server._drain_command_queue()
        assert server.command_queue.empty()
