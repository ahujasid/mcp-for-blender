"""The MCP handshake must not wait for Blender.

`claude mcp add` starts the server and waits for the `initialize` handshake. The
server used to do its startup checks - the addon status, the telemetry event and
the connection to Blender - before the handshake was answered, so a Blender that
was slow to answer (or an addon handshake that waited for it) held the handshake
up. The client then gave up and tore the process down mid-handshake, which it
reports as a stdio error rather than as a timeout (#387).

The checks are informational, so they belong off the handshake's path: they run
in the background, and the tools connect to Blender when they are first used.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from blender_mcp import server


def _stub_the_startup_checks(monkeypatch, connection):
    """Keep the startup checks off the network and off the user's Blender."""
    monkeypatch.setattr(server, "get_blender_connection", connection)
    monkeypatch.setattr(server, "record_startup", lambda: None)
    monkeypatch.setattr(
        server, "check_addon_status_on_startup", lambda: SimpleNamespace(needs_action=False, message="")
    )


def test_the_handshake_does_not_wait_for_blender(monkeypatch):
    """The lifespan is entered while Blender is still unreachable.

    Without the fix the lifespan connects to Blender itself, so it blocks for as
    long as that takes and the client's handshake waits with it.
    """
    reached = threading.Event()
    blocking = threading.Event()
    blocking.set()

    def slow_connection():
        reached.set()
        if blocking.is_set():
            time.sleep(5)
        raise Exception("Blender is not there")

    _stub_the_startup_checks(monkeypatch, slow_connection)

    entered = []

    async def run():
        start = time.monotonic()
        async with server.server_lifespan(None):
            entered.append(time.monotonic() - start)
            # The shutdown's own consent check must not wait for the startup one.
            blocking.clear()

    asyncio.run(run())

    assert entered, "the lifespan never started"
    assert entered[0] < 2.0, f"the handshake waited {entered[0]:.1f}s for Blender"
    # The check still happens, just not on the handshake's path.
    assert reached.wait(10), "the startup check never ran"


def test_the_startup_check_still_reports_a_missing_blender(monkeypatch, caplog):
    """Moving the check to the background must not lose its report."""

    def no_blender():
        raise Exception("Blender is not there")

    _stub_the_startup_checks(monkeypatch, no_blender)

    with caplog.at_level("WARNING"):
        server._startup_checks()

    assert "Could not connect to Blender on startup" in caplog.text
