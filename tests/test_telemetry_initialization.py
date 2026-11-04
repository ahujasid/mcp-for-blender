"""Optional telemetry must not break tools in public source checkouts."""

import asyncio
import builtins
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from blender_mcp import server, telemetry
from blender_mcp.addon_manager import AddonHandshake

DISABLE_VARS = (
    "DISABLE_TELEMETRY",
    "BLENDER_MCP_DISABLE_TELEMETRY",
    "MCP_DISABLE_TELEMETRY",
)


@pytest.fixture
def isolated_collector(monkeypatch):
    for name in DISABLE_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telemetry, "_telemetry_collector", None)
    uuid = Mock(return_value="test-customer")
    worker = Mock()
    monkeypatch.setattr(telemetry.TelemetryCollector, "_get_or_create_uuid", uuid)
    monkeypatch.setattr(telemetry.threading, "Thread", worker)
    post = Mock(side_effect=AssertionError("unexpected telemetry HTTP request"))
    monkeypatch.setattr(telemetry.httpx, "post", post)
    return SimpleNamespace(uuid=uuid, worker=worker, post=post)


def _config_import_error(monkeypatch, missing="blender_mcp.config"):
    real_import = builtins.__import__

    def import_module(name, globals=None, locals=None, fromlist=(), level=0):
        if (
            name == "config"
            and level == 1
            and (globals or {}).get("__package__") == "blender_mcp"
        ):
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_module)


def _installed_config(monkeypatch, enabled=True):
    module = ModuleType("blender_mcp.config")
    module.telemetry_config = SimpleNamespace(enabled=enabled, max_prompt_length=1000)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.telemetry_config


def test_missing_config_disables_collection_without_background_work(
    monkeypatch, isolated_collector
):
    _config_import_error(monkeypatch)

    collector = telemetry.get_telemetry()
    assert not collector.config.enabled
    assert telemetry.get_telemetry() is collector
    assert telemetry.is_telemetry_enabled() is False
    telemetry.record_startup()
    telemetry.record_tool_usage("get_scene_info", True, 1.0)
    assert collector.upload_screenshot(b"private image", "screenshot") == ""
    assert collector._queue.empty()
    isolated_collector.uuid.assert_not_called()
    isolated_collector.worker.assert_not_called()
    isolated_collector.post.assert_not_called()


@pytest.mark.parametrize("name", DISABLE_VARS)
def test_explicit_disable_does_not_import_private_config(
    monkeypatch, isolated_collector, name
):
    monkeypatch.setenv(name, "true")
    # Even an unusable private configuration must not defeat the hard-off switch.
    _config_import_error(monkeypatch, "private_config_dependency")

    collector = telemetry.get_telemetry()
    assert not collector.config.enabled
    isolated_collector.uuid.assert_not_called()
    isolated_collector.worker.assert_not_called()


def test_installed_enabled_configuration_still_starts_collection(
    monkeypatch, isolated_collector
):
    config = _installed_config(monkeypatch)
    collector = telemetry.get_telemetry()

    assert collector.config is config
    assert collector.config.enabled
    isolated_collector.uuid.assert_called_once_with()
    isolated_collector.worker.assert_called_once()
    isolated_collector.worker.return_value.start.assert_called_once_with()


def test_installed_disabled_configuration_stays_inactive(
    monkeypatch, isolated_collector
):
    config = _installed_config(monkeypatch, enabled=False)
    assert telemetry.get_telemetry().config is config
    isolated_collector.uuid.assert_not_called()
    isolated_collector.worker.assert_not_called()


def test_explicit_disable_does_not_mutate_installed_configuration(
    monkeypatch, isolated_collector
):
    config = _installed_config(monkeypatch)
    monkeypatch.setenv("DISABLE_TELEMETRY", "true")
    assert not telemetry.get_telemetry().config.enabled
    assert config.enabled


def test_missing_dependency_inside_installed_config_is_not_hidden(
    monkeypatch, isolated_collector
):
    _config_import_error(monkeypatch, "private_config_dependency")
    with pytest.raises(ModuleNotFoundError) as error:
        telemetry.get_telemetry()
    assert error.value.name == "private_config_dependency"
    isolated_collector.uuid.assert_not_called()
    isolated_collector.worker.assert_not_called()


@pytest.mark.parametrize("consent", [True, False, None])
def test_addon_status_still_reports_consent_without_private_config(
    monkeypatch, isolated_collector, consent
):
    _config_import_error(monkeypatch)
    commands = []

    def send_command(command):
        commands.append(command)
        if command == "get_telemetry_consent":
            if consent is None:
                raise ConnectionError("Blender unavailable")
            return {"consent": consent}
        return {"enabled": False}

    monkeypatch.setattr(
        server,
        "get_blender_connection",
        lambda: SimpleNamespace(send_command=send_command),
    )
    monkeypatch.setattr(server, "_maybe_handshake_addon", lambda blender: None)
    monkeypatch.setattr(server, "_addon_handshake_checked", True)
    monkeypatch.setattr(
        server,
        "_addon_handshake",
        AddonHandshake(True, 13, [1, 8], [], "5.2.2", "native"),
    )

    result, _ = json.JSONDecoder().raw_decode(
        asyncio.run(server.get_addon_status(None))
    )
    assert result["up_to_date"] is True
    assert result["blender_version"] == "5.2.2"
    assert result["telemetry_consent"] is consent
    assert "get_telemetry_consent" in commands
    isolated_collector.post.assert_not_called()
