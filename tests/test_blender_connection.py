"""How the MCP server reports a Blender that is busy or unreachable.

#430: a long operation holds Blender's main thread past the response timeout.
The caller must hear that Blender is busy (not a vague "No data received"), and
the socket must be closed so the addon skips the command if it hasn't run yet.

#431: after a Blender upgrade the addon isn't loaded at all, and a refused
connection was the only symptom. The error must say what's on disk.
"""

import socket

import pytest

from blender_mcp import addon_manager, server


def _listener():
    """A socket that accepts connections and never answers: a busy Blender."""
    sock = socket.socket()
    sock.bind(("localhost", 0))
    sock.listen(1)
    return sock


def test_timeout_reports_busy_blender_and_closes_socket(monkeypatch):
    monkeypatch.setattr(server, "RESPONSE_TIMEOUT", 0.2)
    listener = _listener()
    try:
        conn = server.BlenderConnection(host="localhost", port=listener.getsockname()[1])
        assert conn.connect()
        sock = conn.sock

        with pytest.raises(Exception) as excinfo:
            conn.send_command("execute_code", {"code": "pass"})

        assert str(excinfo.value) == server.TIMEOUT_MESSAGE
        assert conn.sock is None
        assert sock.fileno() == -1, "timed-out socket was dropped without being closed"
    finally:
        listener.close()


def test_connect_failure_names_addon_missing_from_new_blender(monkeypatch):
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]  # closed again: nothing listens here

    report = addon_manager.AddonStatusReport(
        checked=True,
        outdated_paths=[],
        missing=True,
        reason="not_in_newest_blender",
        message="MCP for Blender addon is installed for Blender 5.2 but not for Blender 5.3.",
    )
    monkeypatch.setattr(server, "check_addon_status_on_startup", lambda: report)
    monkeypatch.setattr(server, "_blender_connection", None)
    monkeypatch.setattr(server, "CLI_HOST", "localhost")
    monkeypatch.setattr(server, "CLI_PORT", port)

    with pytest.raises(Exception) as excinfo:
        server.get_blender_connection()

    message = str(excinfo.value)
    assert f"localhost:{port}" in message
    assert "not for Blender 5.3" in message
