"""Any server must work with any addon.

The server updates itself through uvx; the addon only changes when the user
reinstalls it. So this runs the model-facing tools against real addons from
this repository's history - each one's actual command table and execute_code
behaviour, read from its source - and pins the other direction too: today's
addon still answers every command a released server sends.
"""

import ast
import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from blender_mcp import blender_scripts, server
from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from mcp.types import CallToolResult, ImageContent

ROOT = Path(__file__).resolve().parents[1]
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)

# Addons users may still run, oldest first.
HISTORY = {
    "e0e8095": "Apr 2025: no screenshot, execute_code returns no output",
    "25b16af": "Apr 2025: execute_code returns output",
    "aa592fa": "protocol 4: first with a version handshake",
    "f721eb5": "protocol 9: shipped up to 2.0.4",
    "8b4e062": "protocol 11: Premium",
    "current": "this checkout",
}

# Every command a released server (2.1.x) sends. Today's addon must keep
# answering all of them, or upgrading the addon breaks an older server.
RELEASED_SERVER_COMMANDS = {
    "get_scene_info", "get_object_info", "get_viewport_screenshot", "execute_code", "describe_node_type",
    "bpy_api_lookup", "get_addon_info", "get_telemetry_consent", "set_telemetry_consent",
    "list_scene_items", "pick_viewport_object", "get_world_state_snapshot", "drain_human_activity",
    "get_polyhaven_status", "get_polyhaven_categories", "search_polyhaven_assets",
    "get_polyhaven_asset_preview", "download_polyhaven_asset", "set_texture",
    "get_sketchfab_status", "search_sketchfab_models", "get_sketchfab_model_preview", "download_sketchfab_model",
    "get_polypizza_status", "search_polypizza_models", "download_polypizza_model",
    "get_hyper3d_status", "create_rodin_job", "poll_rodin_job_status", "import_generated_asset",
    "get_hunyuan3d_status", "create_hunyuan_job", "poll_hunyuan_job_status", "import_generated_asset_hunyuan",
    "get_tripo_status", "create_tripo_job", "poll_tripo_job_status", "import_generated_asset_tripo",
    "export_scene",
}


def _addon_source(ref: str) -> str:
    if ref == "current":
        return (ROOT / "addon.py").read_text(encoding="utf-8")
    try:
        return subprocess.run(["git", "show", f"{ref}:addon.py"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"addon.py at {ref} isn't available (shallow clone?)")


def _commands(source: str) -> set[str]:
    """Command names an addon dispatches: handler-table keys and cmd_type checks."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                        and isinstance(value, (ast.Attribute, ast.Name)):
                    found.add(key.value)
        if isinstance(node, ast.Compare) and getattr(node.left, "id", None) == "cmd_type":
            found.update(c.value for c in node.comparators if isinstance(c, ast.Constant))
    return found


def _signatures(source: str) -> dict[str, set[str] | None]:
    """Each command's accepted argument names, or None if it takes **kwargs.

    Arguments drift too: a server that sends an argument newer than the addon
    gets "unexpected keyword argument", exactly like a missing command.
    """
    tree = ast.parse(source)
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            args = node.args
            if args.kwarg is not None:
                accepted = None
            else:
                accepted = {a.arg for a in args.args + args.kwonlyargs} - {"self"}
            previous = functions.get(node.name, set())
            # Same name defined twice: accept either signature.
            functions[node.name] = None if accepted is None or previous is None else previous | accepted
    signatures = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    name = getattr(value, "attr", None) or getattr(value, "id", None)
                    if name in functions:
                        signatures[key.value] = functions[name]
    return signatures


def _returns_output(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "execute_code":
            return any(isinstance(n, ast.Constant) and n.value == "result" for n in ast.walk(node))
    return False


def _protocol(source: str) -> int | None:
    match = re.search(r"^ADDON_PROTOCOL_VERSION = (\d+)", source, re.M)
    return int(match.group(1)) if match else None


class HistoricalAddon:
    """Answers like the addon at one commit: unknown commands fail the same way,
    and execute_code returns printed output only if that addon did."""

    def __init__(self, source: str):
        self.commands = _commands(source)
        self.signatures = _signatures(source)
        self.returns_output = _returns_output(source)
        self.protocol = _protocol(source)
        self.sent = []

    def send_command(self, command, params=None, read_only=False):
        self.sent.append(command)
        if command not in self.commands:
            raise Exception(f"Communication error with Blender: Unknown command type: {command}")
        params = params or {}
        accepted = self.signatures.get(command)
        unexpected = sorted(set(params) - accepted) if accepted is not None else []
        if unexpected:
            raise Exception(f"Communication error with Blender: {command}() got an unexpected keyword "
                            f"argument '{unexpected[0]}'")
        if command == "execute_code":
            return self._execute(params["code"])
        if command == "get_viewport_screenshot":
            Path(params["filepath"]).write_bytes(PNG)
            return {"success": True, "width": 1, "height": 1}
        if command == "get_addon_info":
            return {"protocol_version": self.protocol, "addon_version": [1, 0], "capabilities": [],
                    "blender_version": "4.2.0", "premium_generators": []}
        if command.startswith("get_") and command.endswith("_status"):
            return {"enabled": True, "message": "ready"}
        if command == "get_scene_info":
            return {"name": "Scene", "object_count": 0, "objects": []}
        if command.startswith("search_"):
            return {"results": [], "assets": [], "total_count": 0, "returned_count": 0}
        if command.startswith("download_"):
            return {"success": True, "imported_objects": ["Model"]}
        return {}

    def _execute(self, code):
        if not self.returns_output:
            return {"executed": True}
        literal = re.search(r"^ARGS = _json\.loads\((.*)\)$", code, re.M)
        if not literal:
            return {"executed": True, "result": ""}
        args = json.loads(json.loads(literal.group(1)))
        if "filepath" in args:  # look
            Path(args["filepath"]).write_bytes(PNG)
            result = {"mode": args["mode"], "targets": 1, "center": [0, 0, 0], "size": [1, 1, 1]}
        elif "names" in args:  # bounds
            result = []
        else:  # scene summary
            result = {"header": {"scene": "Scene", "file": "(unsaved)", "blender": "4.2.0", "engine": "CYCLES",
                                 "frames": [1, 250, 1], "fps": 24, "resolution": [1920, 1080], "camera": None,
                                 "world_hdri": None, "unit_scale": 1.0, "object_counts": {}, "selected": [],
                                 "active": None, "mode": "OBJECT"}, "lines": [], "total": 0, "shown": 0}
        return {"executed": True, "result": blender_scripts.RESULT_MARKER + json.dumps(result)}


@pytest.fixture(params=list(HISTORY), ids=list(HISTORY))
def addon(request, monkeypatch):
    fake = HistoricalAddon(_addon_source(request.param))
    monkeypatch.setattr(server, "get_blender_connection", lambda: fake)
    up_to_date = fake.protocol is not None and fake.protocol >= EXPECTED_ADDON_PROTOCOL_VERSION
    monkeypatch.setattr(server, "_addon_handshake", AddonHandshake(
        up_to_date, fake.protocol, [1, 0], [], "4.2.0", "native" if fake.protocol else "error"))
    return fake


def _run(coro_or_value):
    return asyncio.run(coro_or_value) if asyncio.iscoroutine(coro_or_value) else coro_or_value


def _has_image(result) -> bool:
    return isinstance(result, CallToolResult) and any(isinstance(c, ImageContent) for c in result.content)


def _text(result) -> str:
    if isinstance(result, str):
        return result
    return " ".join(getattr(c, "text", "") for c in result.content)


# ------------------------------------------------------------ new server, any addon

@pytest.mark.parametrize("mode", ["viewport", "angles", "camera", "frames"])
def test_look_always_shows_something_or_says_how_to_fix_it(addon, mode):
    result = _run(server.look(None, mode=mode))
    can_see = "get_viewport_screenshot" in addon.commands or addon.returns_output
    if can_see:
        assert _has_image(result), _text(result)
    else:
        assert result.isError and "install-addon" in _text(result)
    if mode != "viewport" and not addon.returns_output and can_see:
        assert "plain viewport" in _text(result)


def test_scene_info_always_answers(addon):
    reply = _run(server.get_scene_info(None))
    assert not reply.startswith("Error"), reply


def test_addon_status_always_answers(addon):
    reply = _run(server.get_addon_status(None))
    assert not reply.startswith("Error"), reply


@pytest.mark.parametrize("source, command", [
    ("polyhaven", "search_polyhaven_assets"),
    ("sketchfab", "search_sketchfab_models"),
    ("polypizza", "search_polypizza_models"),
])
def test_asset_search_works_or_asks_for_an_update(addon, source, command):
    reply = _text(_run(server.search_assets(None, source=source, query="chair")))
    accepted = addon.signatures.get(command, set())
    works = command in addon.commands and (accepted is None or {"query"} <= accepted or source == "sketchfab")
    if works:
        assert "Unknown command" not in reply and "unexpected keyword" not in reply, reply
    else:
        assert "install-addon" in reply, reply
    assert "unexpected keyword" not in reply and "Traceback" not in reply


def test_asset_import_works_or_asks_for_an_update(addon):
    reply = _run(server.import_asset(None, source="polypizza", id="abc", target_size=1.0))
    if "download_polypizza_model" in addon.commands:
        assert not reply.startswith("Error"), reply
    else:
        assert "install-addon" in reply, reply


def test_generation_never_raises(addon):
    for provider in ("auto", "tripo", "hunyuan3d", "hyper3d"):
        reply = _run(server.generate_3d(None, prompt="a stool", provider=provider, wait_seconds=10))
        assert isinstance(reply, str) and reply


# ------------------------------------------------------------ any server, new addon

def test_current_addon_answers_every_command_released_servers_send():
    missing = RELEASED_SERVER_COMMANDS - _commands(_addon_source("current"))
    assert not missing, f"Removing these breaks older servers: {sorted(missing)}"


def _server_commands() -> set[str]:
    """Command names this package can send, read from its source."""
    found = set()
    for path in (ROOT / "src" / "blender_mcp").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in ("send_command", "send", "_generation_send"):
                    found.add(node.args[0].value)
    # get_{name}_status, built from a list.
    found.update(f"get_{n}_status" for n in ("polyhaven", "sketchfab", "polypizza", "hunyuan3d", "hyper3d"))
    return found


def test_every_command_the_server_sends_exists_in_the_current_addon():
    # A new server command needs the addon change to ship first, and a fallback
    # for addons without it (see the tests above).
    missing = _server_commands() - _commands(_addon_source("current"))
    assert not missing, sorted(missing)
