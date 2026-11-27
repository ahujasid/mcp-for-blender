"""Regression tests for the add-on's deferred socket auto-start lifecycle.

The add-on cannot be imported without Blender, so the small auto-start helper
functions are lifted out with AST and executed against a minimal bpy stub.
"""

from __future__ import annotations

import ast
import ipaddress
import socket
import types
from typing import ClassVar

import pytest
from conftest import ROOT_ADDON

_AUTOSTART_FUNCTIONS = {
    "_blendermcp_bind_address",
    "_blendermcp_port_has_listener",
    "_blendermcp_ensure_server_running",
    "_blendermcp_schedule_auto_start",
    "_blendermcp_load_post",
    "_blendermcp_register_auto_start",
    "_blendermcp_unregister_auto_start",
}


class _Timers:
    def __init__(self):
        self.registrations = {}

    def register(self, fn, first_interval=0.0, persistent=False):
        self.registrations[fn] = {
            "first_interval": first_interval,
            "persistent": persistent,
        }

    def unregister(self, fn):
        self.registrations.pop(fn)

    def is_registered(self, fn):
        return fn in self.registrations


class _Server:
    instances: ClassVar[list[_Server]] = []
    failures_before_start = 0

    def __init__(self, port, host="localhost"):
        self.host = host
        self.port = port
        self.running = False
        self.start_attempts = 0
        self.__class__.instances.append(self)

    def start(self):
        self.start_attempts += 1
        if self.start_attempts > self.__class__.failures_before_start:
            self.running = True


def _load_autostart_helpers():
    source = ROOT_ADDON.read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _AUTOSTART_FUNCTIONS
    ]
    assert {node.name for node in body} == _AUTOSTART_FUNCTIONS

    timers = _Timers()
    scene = types.SimpleNamespace(
        blendermcp_auto_start_server=True,
        blendermcp_port=9876,
        blendermcp_server_running=False,
    )
    bpy = types.SimpleNamespace(
        app=types.SimpleNamespace(
            background=False,
            timers=timers,
            handlers=types.SimpleNamespace(load_post=[]),
        ),
        context=types.SimpleNamespace(scene=scene),
        types=types.SimpleNamespace(),
    )
    namespace = {
        "bpy": bpy,
        "socket": socket,
        "ipaddress": ipaddress,
        "persistent": lambda fn: fn,
        "BlenderMCPServer": _Server,
        "get_blendermcp_addon_preferences": lambda context=None: None,
        "_user_stopped_server": False,
    }
    exec(  # noqa: S102 - execute only the selected functions from our own source.
        compile(ast.Module(body=body, type_ignores=[]), "<addon>", "exec"), namespace
    )
    namespace["_blendermcp_port_has_listener"] = lambda host, port: False
    return namespace, bpy, timers, scene


def setup_function():
    _Server.instances = []
    _Server.failures_before_start = 0


def test_registration_defers_start_and_is_idempotent():
    namespace, bpy, timers, _scene = _load_autostart_helpers()
    register_auto_start = namespace["_blendermcp_register_auto_start"]
    ensure_running = namespace["_blendermcp_ensure_server_running"]
    load_post = namespace["_blendermcp_load_post"]

    register_auto_start()
    register_auto_start()

    assert _Server.instances == []
    assert bpy.app.handlers.load_post == [load_post]
    assert timers.registrations == {
        ensure_running: {"first_interval": 0.5, "persistent": True}
    }


def test_timer_retries_a_failed_start_then_marks_scene_running():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    ensure_running = namespace["_blendermcp_ensure_server_running"]
    _Server.failures_before_start = 1

    assert ensure_running() == 1.0
    server = bpy.types.blendermcp_server
    assert server.start_attempts == 1
    assert scene.blendermcp_server_running is False

    assert ensure_running() is None
    assert server.start_attempts == 2
    assert scene.blendermcp_server_running is True


def test_load_post_restores_a_missing_timer():
    namespace, _bpy, timers, _scene = _load_autostart_helpers()
    ensure_running = namespace["_blendermcp_ensure_server_running"]

    namespace["_blendermcp_load_post"](None)

    assert timers.is_registered(ensure_running)
    assert timers.registrations[ensure_running]["persistent"] is True


def test_disabled_auto_start_does_not_create_a_server():
    namespace, _bpy, _timers, scene = _load_autostart_helpers()
    scene.blendermcp_auto_start_server = False

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert _Server.instances == []


def test_occupied_port_does_not_create_a_competing_server():
    namespace, _bpy, _timers, scene = _load_autostart_helpers()
    namespace["_blendermcp_port_has_listener"] = lambda host, port: True

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert _Server.instances == []
    assert scene.blendermcp_server_running is False


def test_background_mode_does_not_schedule_retries():
    namespace, bpy, _timers, _scene = _load_autostart_helpers()
    bpy.app.background = True

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert _Server.instances == []


def test_missing_scene_retries_without_binding_the_default_port():
    namespace, bpy, _timers, _scene = _load_autostart_helpers()
    bpy.context.scene = None

    assert namespace["_blendermcp_ensure_server_running"]() == 0.5
    assert _Server.instances == []


def test_stopped_server_retargets_to_the_loaded_scene_port():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    server = _Server(port=9876)
    bpy.types.blendermcp_server = server
    scene.blendermcp_port = 9999

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert server.port == 9999
    assert server.running is True


def test_auto_start_uses_the_configured_bind_address():
    namespace, bpy, _timers, _scene = _load_autostart_helpers()
    prefs = types.SimpleNamespace(
        bind_address="192.0.2.10", allow_remote_connections=True
    )
    namespace["get_blendermcp_addon_preferences"] = lambda context=None: prefs
    probes = []
    namespace["_blendermcp_port_has_listener"] = lambda host, port: (
        probes.append((host, port)) or False
    )

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert bpy.types.blendermcp_server.host == "192.0.2.10"
    assert probes == [("192.0.2.10", 9876)]


def _set_bind_preferences(namespace, host, allow_remote=False):
    prefs = types.SimpleNamespace(
        bind_address=host, allow_remote_connections=allow_remote
    )
    namespace["get_blendermcp_addon_preferences"] = lambda context=None: prefs


@pytest.mark.parametrize(
    "host, allow_remote, expected",
    [
        ("localhost", False, "localhost"),
        (" LOCALHOST ", False, "localhost"),
        ("127.0.0.1", False, "127.0.0.1"),
        ("127.0.0.2", False, "127.0.0.2"),
        ("192.0.2.10", True, "192.0.2.10"),
        (" 192.0.2.10 ", True, "192.0.2.10"),
        ("0.0.0.0", True, "0.0.0.0"),
    ],
)
def test_bind_address_validation(host, allow_remote, expected):
    namespace, _bpy, _timers, _scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, host, allow_remote)

    assert namespace["_blendermcp_bind_address"]() == expected


@pytest.mark.parametrize("host", ["192.0.2.10", "0.0.0.0", "8.8.8.8"])
def test_non_loopback_address_requires_explicit_opt_in(host):
    namespace, _bpy, _timers, _scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, host)

    with pytest.raises(ValueError, match="Allow Remote Connections"):
        namespace["_blendermcp_bind_address"]()


@pytest.mark.parametrize(
    "host", ["", "::1", "localhost.example", "256.0.0.1", "192.0.2.10:9876"]
)
def test_invalid_or_unsupported_bind_address_is_rejected(host):
    namespace, _bpy, _timers, _scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, host, allow_remote=True)

    with pytest.raises(ValueError, match="localhost or an IPv4 address"):
        namespace["_blendermcp_bind_address"]()


def test_missing_preferences_keep_the_loopback_default():
    namespace, bpy, _timers, _scene = _load_autostart_helpers()

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert bpy.types.blendermcp_server.host == "localhost"


@pytest.mark.parametrize("host", ["0.0.0.0", "not-an-address"])
def test_auto_start_does_not_bind_when_configuration_is_rejected(host):
    namespace, _bpy, _timers, scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, host)

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert _Server.instances == []
    assert scene.blendermcp_server_running is False


def test_stopped_server_uses_updated_bind_preferences():
    namespace, bpy, _timers, _scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, "127.0.0.2")
    server = _Server(port=9876)
    bpy.types.blendermcp_server = server

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert server.host == "127.0.0.2"
    assert server.running is True


def test_running_server_is_not_retargeted_by_preference_or_scene_changes():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    server = _Server(host="127.0.0.1", port=9876)
    server.start()
    bpy.types.blendermcp_server = server
    _set_bind_preferences(namespace, "0.0.0.0")
    scene.blendermcp_port = 9999

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert (server.host, server.port) == ("127.0.0.1", 9876)
    assert server.start_attempts == 1
    assert scene.blendermcp_server_running is True


def _manual_connect(namespace, context):
    """Execute the real operator method without importing Blender's UI types."""
    tree = ast.parse(ROOT_ADDON.read_text(encoding="utf-8"))
    operator = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BLENDERMCP_OT_StartServer"
    )
    execute = next(
        node
        for node in operator.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute"
    )
    exec(  # noqa: S102 - execute only the operator from our own add-on source.
        compile(ast.Module(body=[execute], type_ignores=[]), "<addon>", "exec"),
        namespace,
    )
    reports = []
    instance = types.SimpleNamespace(
        report=lambda level, message: reports.append((level, message))
    )
    result = namespace["execute"](instance, context)
    return result, reports


def test_manual_connect_uses_the_same_persistent_address_as_auto_start():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, "192.0.2.10", allow_remote=True)
    namespace["_user_stopped_server"] = True

    result, reports = _manual_connect(namespace, bpy.context)

    assert result == {"FINISHED"}
    assert reports == []
    assert (bpy.types.blendermcp_server.host, bpy.types.blendermcp_server.port) == (
        "192.0.2.10",
        9876,
    )
    assert scene.blendermcp_server_running is True
    assert namespace["_user_stopped_server"] is False


@pytest.mark.parametrize("host", ["0.0.0.0", "invalid"])
def test_manual_connect_rejects_invalid_configuration_without_clearing_disconnect(host):
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, host)
    namespace["_user_stopped_server"] = True

    result, reports = _manual_connect(namespace, bpy.context)

    assert result == {"CANCELLED"}
    assert reports and reports[0][0] == {"ERROR"}
    assert _Server.instances == []
    assert scene.blendermcp_server_running is False
    assert namespace["_user_stopped_server"] is True


def test_manual_connect_updates_a_stopped_server_endpoint():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    _set_bind_preferences(namespace, "127.0.0.2")
    server = _Server(port=9876)
    bpy.types.blendermcp_server = server
    scene.blendermcp_port = 9999

    result, _reports = _manual_connect(namespace, bpy.context)

    assert result == {"FINISHED"}
    assert (server.host, server.port) == ("127.0.0.2", 9999)
    assert server.start_attempts == 1


def test_manual_connect_does_not_retarget_a_running_server():
    namespace, bpy, _timers, scene = _load_autostart_helpers()
    server = _Server(port=9876)
    server.start()
    bpy.types.blendermcp_server = server
    _set_bind_preferences(namespace, "0.0.0.0")
    scene.blendermcp_port = 9999

    result, reports = _manual_connect(namespace, bpy.context)

    assert result == {"FINISHED"}
    assert reports == []
    assert (server.host, server.port) == ("localhost", 9876)
    assert server.start_attempts == 1


def test_manual_disconnect_survives_loading_another_file():
    namespace, _bpy, _timers, scene = _load_autostart_helpers()
    namespace["_user_stopped_server"] = True

    assert namespace["_blendermcp_ensure_server_running"]() is None
    assert _Server.instances == []
    assert scene.blendermcp_server_running is False


def test_registration_clears_manual_disconnect_for_an_addon_reload():
    namespace, _bpy, _timers, _scene = _load_autostart_helpers()
    namespace["_user_stopped_server"] = True

    namespace["_blendermcp_register_auto_start"]()

    assert namespace["_user_stopped_server"] is False


def test_unregistration_removes_handler_and_pending_timer():
    namespace, bpy, timers, _scene = _load_autostart_helpers()
    register_auto_start = namespace["_blendermcp_register_auto_start"]
    unregister_auto_start = namespace["_blendermcp_unregister_auto_start"]

    register_auto_start()
    unregister_auto_start()

    assert bpy.app.handlers.load_post == []
    assert timers.registrations == {}
