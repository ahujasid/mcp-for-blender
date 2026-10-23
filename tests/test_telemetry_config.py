"""Source checkouts gitignore config.py. The server still has to start."""

import asyncio
import sys
import types

from blender_mcp import server, telemetry


def _block_config(monkeypatch):
    """Make `from blender_mcp.config import telemetry_config` fail as a missing file."""
    monkeypatch.setitem(sys.modules, "blender_mcp.config", None)
    for var in ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telemetry, "_telemetry_collector", None)


class _Blender:
    def send_command(self, command, params=None, read_only=False):
        if command == "get_addon_info":
            return {
                "protocol_version": server.EXPECTED_ADDON_PROTOCOL_VERSION,
                "addon_version": [2, 2, 2],
                "capabilities": [],
                "blender_version": "5.2.2",
                "premium_generators": [],
            }
        if command == "get_telemetry_consent":
            return {"consent": False}
        if command.startswith("get_") and command.endswith("_status"):
            return {"enabled": False}
        raise AssertionError(command)


def test_get_addon_status_without_config_py(monkeypatch):
    """Importing telemetry and reading addon status must not require config.py."""
    _block_config(monkeypatch)
    monkeypatch.setattr(server, "get_blender_connection", lambda: _Blender())
    monkeypatch.setattr(server, "_addon_handshake", None)
    monkeypatch.setattr(server, "_addon_handshake_checked", False)

    reply = asyncio.run(server.get_addon_status(None))

    assert not reply.startswith("Error"), reply
    assert "No module named" not in reply
    assert "5.2.2" in reply
    assert '"telemetry_consent": false' in reply
    assert telemetry.get_telemetry().config.enabled is False


def test_disable_env_skips_config_import(monkeypatch):
    """DISABLE_TELEMETRY is checked before config.py is loaded."""
    monkeypatch.setenv("DISABLE_TELEMETRY", "true")
    monkeypatch.setattr(telemetry, "_telemetry_collector", None)

    class Sentinel(types.ModuleType):
        def __getattr__(self, name):
            raise AssertionError(f"loaded config.{name} while telemetry is disabled")

    monkeypatch.setitem(sys.modules, "blender_mcp.config", Sentinel("blender_mcp.config"))

    collector = telemetry.TelemetryCollector()
    assert collector.config.enabled is False


def test_addon_status_error_names_the_exception(monkeypatch):
    """A failure says what broke, not only the exception's message."""
    def fail():
        raise RuntimeError("socket closed")

    monkeypatch.setattr(server, "get_blender_connection", fail)
    reply = asyncio.run(server.get_addon_status(None))
    assert reply.startswith("Error checking addon status:")
    assert "RuntimeError: socket closed" in reply
    assert "assembling the status report" in reply
    assert reply != "Error checking addon status: socket closed"
