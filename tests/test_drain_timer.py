"""The command-queue timer is registered and unregistered as one object.

bpy.app.timers keys a timer by the identity of the callable it was given, and
each `server._drain_command_queue` lookup is a new bound method. Checked that
way, is_registered() was always False and unregister() always raised, so
stop() never removed the timer and a quick restart left two of them running.
"""
import pytest

from test_meshy import _load_addon


class IdentityTimers:
    """bpy.app.timers as Blender implements it: keyed by the callable's identity."""

    def __init__(self):
        self.live = {}

    def register(self, function, first_interval=0.0, persistent=False):
        self.live[id(function)] = function

    def is_registered(self, function):
        return id(function) in self.live

    def unregister(self, function):
        if id(function) not in self.live:
            raise ValueError("function is not registered")
        del self.live[id(function)]

    def drains(self, server):
        return [f for f in self.live.values() if getattr(f, "__func__", None) is type(server)._drain_command_queue]


@pytest.fixture
def server(monkeypatch):
    addon, bpy, _scene = _load_addon(monkeypatch)
    timers = IdentityTimers()
    monkeypatch.setattr(bpy.app, "timers", timers)
    server = addon.BlenderMCPServer(host="127.0.0.1", port=0)
    yield server, timers
    server.stop()


def test_stop_removes_the_timer_start_added(server):
    server, timers = server
    server.start()
    assert len(timers.drains(server)) == 1

    server.stop()

    assert timers.drains(server) == []


def test_a_quick_restart_leaves_one_timer(server):
    server, timers = server
    for _ in range(3):
        server.start()
        server.stop()
    server.start()

    assert len(timers.drains(server)) == 1


def test_start_twice_registers_once(server):
    server, timers = server
    server.start()
    server.running = False  # start() refuses while running; this probes its timer guard alone
    server.socket.close()
    server.start()

    assert len(timers.drains(server)) == 1
