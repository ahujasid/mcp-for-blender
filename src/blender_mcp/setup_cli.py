"""
`mcp-for-blender setup`: configure the user's MCP clients and the Blender addon.

Detects installed clients, adds a `blender` server entry that runs uvx by its
absolute path (GUI apps don't inherit the terminal's PATH, hence `spawn uvx
ENOENT`), installs the addon, and enables it by running Blender headless.

Config files are merged, never replaced: other servers are kept and a .bak of
the previous file is written beside it. Claude Code and Codex are configured
through their own CLIs, which own those files.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .addon_manager import (
    blender_config_base,
    find_existing_addon_installs,
    install_addon,
)

PACKAGE = "mcp-for-blender"
OLD_PACKAGE = "blender-mcp"
SERVER_NAME = "blender"
# What the Codex plugin sets, so a plain Codex entry gets the same viewport
# app and asset pickers.
CODEX_ENV = {"BLENDER_MCP_APPS": "1", "BLENDER_MCP_OPENAI_FORMS": "1"}
CODEX_PLUGIN_NAME = "mcp-for-blender"

_NEW_RE = re.compile(r"(?<![\w-])mcp-for-blender(?![\w-])")
_OLD_RE = re.compile(r"(?<![\w-])blender-mcp(?![\w-])")

# status values
NEW = "new"                # no entry yet: add one
CONFIGURED = "configured"  # already runs mcp-for-blender: leave it alone
OUTDATED = "outdated"      # runs the old blender-mcp package: offer to update
UNREADABLE = "unreadable"  # config exists but isn't valid JSON: don't touch


@dataclass
class Client:
    key: str
    name: str
    # json: we edit `config_path` ourselves; claude-code / codex: their CLI does
    kind: str
    config_path: Path | None = None
    # mcpServers | vscode | opencode
    style: str = "mcpServers"
    restart_hint: str = ""
    cli_path: str | None = None


@dataclass
class ClientState:
    status: str
    entry_key: str | None = None
    detail: str = ""


@dataclass
class ClientResult:
    client: Client
    ok: bool
    message: str
    backup: Path | None = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _xdg_config(env) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _appdata(env) -> Path:
    appdata = env.get("APPDATA")
    return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"


def _claude_desktop_dir(platform, env) -> Path:
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    if platform == "win32":
        # The Microsoft Store (MSIX) build has its AppData redirected into its
        # package folder and never reads %APPDATA%\Claude.
        local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        msix = sorted(glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")))
        if msix:
            return Path(msix[0])
        return _appdata(env) / "Claude"
    return _xdg_config(env) / "Claude"


def _vscode_user_dir(platform, env) -> Path:
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    if platform == "win32":
        return _appdata(env) / "Code" / "User"
    return _xdg_config(env) / "Code" / "User"


def _devin_config(platform, env) -> Path | None:
    """Devin Desktop (formerly Windsurf) config, or None if it isn't installed.

    It moved to ~/.config/devin in June 2026 and still reads the legacy
    ~/.codeium/windsurf folder, so write wherever the user's install lives.
    """
    new_dir = _appdata(env) / "devin" if platform == "win32" else _xdg_config(env) / "devin"
    legacy_dir = Path.home() / ".codeium" / "windsurf"
    if new_dir.is_dir():
        return new_dir / "mcp_config.json"
    if legacy_dir.is_dir():
        return legacy_dir / "mcp_config.json"
    return None


def _antigravity_config() -> Path | None:
    """Antigravity 2.0 shares ~/.gemini/config; earlier builds used ~/.gemini/antigravity."""
    gemini = Path.home() / ".gemini"
    if (gemini / "config").is_dir():
        return gemini / "config" / "mcp_config.json"
    if (gemini / "antigravity").is_dir():
        return gemini / "antigravity" / "mcp_config.json"
    return None


def _opencode_config(env, which) -> Path | None:
    config_dir = _xdg_config(env) / "opencode"
    if not config_dir.is_dir() and not which("opencode"):
        return None
    for name in ("opencode.json", "opencode.jsonc"):
        if (config_dir / name).is_file():
            return config_dir / name
    return config_dir / "opencode.json"


def detect_clients(platform: str | None = None, env=None, which=shutil.which) -> list[Client]:
    """Clients installed on this machine, in the order they're offered."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    quit_hint = (
        "quit it from the system tray (right-click → Quit)" if platform == "win32"
        else "press Cmd+Q" if platform == "darwin" else "quit it fully"
    )
    clients: list[Client] = []

    claude_dir = _claude_desktop_dir(platform, env)
    if claude_dir.is_dir():
        clients.append(Client(
            "claude-desktop", "Claude Desktop", "json",
            claude_dir / "claude_desktop_config.json",
            restart_hint=f"Fully quit Claude Desktop ({quit_hint}) and reopen it.",
        ))

    claude_cli = which("claude")
    if claude_cli:
        clients.append(Client(
            "claude-code", "Claude Code", "claude-code", cli_path=claude_cli,
            restart_hint="Start a new Claude Code session.",
        ))

    codex_cli = which("codex")
    if codex_cli:
        clients.append(Client(
            "codex", "Codex", "codex", cli_path=codex_cli,
            restart_hint="Restart Codex (CLI, desktop app and IDE extension share this setting).",
        ))

    if (Path.home() / ".cursor").is_dir():
        clients.append(Client(
            "cursor", "Cursor", "json", Path.home() / ".cursor" / "mcp.json",
            restart_hint="Restart Cursor.",
        ))

    vscode_dir = _vscode_user_dir(platform, env)
    if vscode_dir.is_dir():
        clients.append(Client(
            "vscode", "VS Code", "json", vscode_dir / "mcp.json", style="vscode",
            restart_hint="Restart VS Code. The first time, it may ask you to trust "
                         "and start the server (Command Palette → MCP: List Servers).",
        ))

    devin = _devin_config(platform, env)
    if devin:
        clients.append(Client(
            "devin", "Devin Desktop (Windsurf)", "json", devin,
            restart_hint="Restart Devin Desktop / Windsurf.",
        ))

    opencode = _opencode_config(env, which)
    if opencode:
        clients.append(Client(
            "opencode", "OpenCode", "json", opencode, style="opencode",
            restart_hint="Restart OpenCode.",
        ))

    antigravity = _antigravity_config()
    if antigravity:
        clients.append(Client(
            "antigravity", "Antigravity", "json", antigravity,
            restart_hint="Restart Antigravity.",
        ))

    return clients


def find_uvx(which=shutil.which) -> str | None:
    """Absolute path to uvx, including uv's default install folders not yet on PATH."""
    found = which("uvx")
    if found:
        # Not resolved: Homebrew's /opt/homebrew/bin/uvx is a symlink into a
        # versioned Cellar folder that disappears on the next `brew upgrade`.
        return os.path.abspath(found)
    home = Path.home()
    exe = "uvx.exe" if sys.platform == "win32" else "uvx"
    candidates = [home / ".local" / "bin" / exe, home / ".cargo" / "bin" / exe]
    if sys.platform != "win32":
        candidates += [Path("/opt/homebrew/bin/uvx"), Path("/usr/local/bin/uvx")]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


# ---------------------------------------------------------------------------
# JSON config files
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    pass


def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas, leaving strings intact."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_json_config(path: Path) -> tuple[dict, bool]:
    """(config, had_comments). A missing or empty file is an empty config."""
    if not path.is_file():
        return {}, False
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}, False
    try:
        data, had_comments = json.loads(text), False
    except json.JSONDecodeError:
        try:
            data, had_comments = json.loads(_strip_jsonc(text)), True
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} isn't valid JSON ({e}); left it unchanged") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doesn't hold a JSON object; left it unchanged")
    return data, had_comments


def _servers_key(style: str) -> str:
    return {"vscode": "servers", "opencode": "mcp"}.get(style, "mcpServers")


def server_entry(style: str, uvx: str) -> dict:
    if style == "vscode":
        return {"type": "stdio", "command": uvx, "args": [PACKAGE]}
    if style == "opencode":
        return {"type": "local", "command": [uvx, PACKAGE], "enabled": True}
    return {"command": uvx, "args": [PACKAGE]}


def _entry_text(entry) -> str:
    """The command line an entry runs, to tell which package it points at."""
    if not isinstance(entry, dict):
        return ""
    parts = entry.get("command")
    parts = list(parts) if isinstance(parts, list) else [parts]
    args = entry.get("args")
    if isinstance(args, list):
        parts += args
    return " ".join(str(p) for p in parts if p)


def servers_state(servers: dict) -> ClientState:
    """Whether a client's server map already runs this package."""
    old_key = None
    for key, entry in servers.items():
        text = _entry_text(entry)
        if _NEW_RE.search(text):
            return ClientState(CONFIGURED, key)
        if _OLD_RE.search(text) and old_key is None:
            old_key = key
    if old_key is not None:
        return ClientState(OUTDATED, old_key)
    return ClientState(NEW)


def json_client_state(client: Client) -> ClientState:
    try:
        config, _ = read_json_config(client.config_path)
    except (ConfigError, OSError) as e:
        return ClientState(UNREADABLE, detail=str(e))
    servers = config.get(_servers_key(client.style))
    return servers_state(servers if isinstance(servers, dict) else {})


def _free_key(servers: dict) -> str:
    """`blender`, unless a different server already uses that name."""
    key, n = SERVER_NAME, 2
    while key in servers:
        key, n = f"{SERVER_NAME}-{n}", n + 1
    return key


def configure_json_client(client: Client, uvx: str, state: ClientState) -> ClientResult:
    path = client.config_path
    try:
        config, had_comments = read_json_config(path)
    except (ConfigError, OSError) as e:
        return ClientResult(client, False, str(e))

    servers_key = _servers_key(client.style)
    servers = config.get(servers_key)
    if servers is None:
        servers = config[servers_key] = {}
    elif not isinstance(servers, dict):
        return ClientResult(client, False, f'"{servers_key}" in {path} isn\'t an object; left it unchanged')

    key = state.entry_key if state.status == OUTDATED else _free_key(servers)
    entry = server_entry(client.style, uvx)
    if state.status == OUTDATED and isinstance(servers.get(key), dict):
        # Keep the user's env, enabled flag and so on; replace only the command.
        entry = {**servers[key], **entry}
    servers[key] = entry
    if client.style == "opencode":
        config.setdefault("$schema", "https://opencode.ai/config.json")

    backup = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.replace(staged, path)
    except OSError as e:
        return ClientResult(client, False, f"Couldn't write {path}: {e}")

    verb = "Updated" if state.status == OUTDATED else "Added"
    message = f'{verb} "{key}" in {path}'
    if had_comments:
        message += " (comments in that file were removed; the original is in the .bak)"
    return ClientResult(client, True, message, backup)


# ---------------------------------------------------------------------------
# Claude Code and Codex (configured through their CLIs)
# ---------------------------------------------------------------------------


def _claude_json_path(env) -> Path:
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"


def claude_code_state(env=None) -> ClientState:
    """User-scope servers live in the top-level mcpServers of ~/.claude.json."""
    env = os.environ if env is None else env
    try:
        config, _ = read_json_config(_claude_json_path(env))
    except (ConfigError, OSError) as e:
        return ClientState(UNREADABLE, detail=str(e))
    servers = config.get("mcpServers")
    return servers_state(servers if isinstance(servers, dict) else {})


def _codex_home(env) -> Path:
    home = env.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


_TOML_HEADER_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$", re.MULTILINE)


def _codex_mcp_sections(text: str) -> dict[str, str]:
    """{server name: its table body} for [mcp_servers.<name>] tables in config.toml.

    A regex rather than a TOML parser: tomllib only arrived in Python 3.11,
    and all this needs is each table's name and the text beneath it.
    """
    headers = list(_TOML_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, match in enumerate(headers):
        parts = [p.strip().strip('"\'') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", match.group(1))]
        if len(parts) < 2 or parts[0] != "mcp_servers":
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        sections[parts[1]] = sections.get(parts[1], "") + text[match.end():end]
    return sections


def codex_plugin_installed(env=None) -> bool:
    env = os.environ if env is None else env
    home = _codex_home(env)
    if glob.glob(str(home / "plugins" / "cache" / "*" / CODEX_PLUGIN_NAME)):
        return True
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r'^\s*\[\s*plugins\.\s*"' + re.escape(CODEX_PLUGIN_NAME) + "@", text, re.MULTILINE) is not None


def codex_state(env=None) -> ClientState:
    env = os.environ if env is None else env
    if codex_plugin_installed(env):
        return ClientState(CONFIGURED, CODEX_PLUGIN_NAME, detail="MCP for Blender plugin installed")
    try:
        text = (_codex_home(env) / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ClientState(NEW)
    except OSError as e:
        return ClientState(UNREADABLE, detail=str(e))
    old_key = None
    for name, body in _codex_mcp_sections(text).items():
        if _NEW_RE.search(body):
            return ClientState(CONFIGURED, name)
        if _OLD_RE.search(body) and old_key is None:
            old_key = name
    return ClientState(OUTDATED, old_key) if old_key else ClientState(NEW)


def _run_cli(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def _cli_commands(client: Client, uvx: str, state: ClientState) -> list[list[str]]:
    key = state.entry_key if state.status == OUTDATED else SERVER_NAME
    commands: list[list[str]] = []
    if client.kind == "claude-code":
        if state.status == OUTDATED:
            commands.append([client.cli_path, "mcp", "remove", "--scope", "user", key])
        commands.append([client.cli_path, "mcp", "add", "--scope", "user", key, "--", uvx, PACKAGE])
    else:
        if state.status == OUTDATED:
            commands.append([client.cli_path, "mcp", "remove", key])
        env_flags = [flag for k, v in CODEX_ENV.items() for flag in ("--env", f"{k}={v}")]
        commands.append([client.cli_path, "mcp", "add", key, *env_flags, "--", uvx, PACKAGE])
    return commands


def configure_cli_client(client: Client, uvx: str, state: ClientState) -> ClientResult:
    for command in _cli_commands(client, uvx, state):
        ok, output = _run_cli(command)
        if not ok:
            shown = " ".join(Path(command[0]).name if i == 0 else a for i, a in enumerate(command))
            return ClientResult(client, False, f"`{shown}` failed: {output or 'no output'}")
    verb = "Updated" if state.status == OUTDATED else "Added"
    key = state.entry_key if state.status == OUTDATED else SERVER_NAME
    where = "user scope, all projects" if client.kind == "claude-code" else "~/.codex/config.toml"
    return ClientResult(client, True, f'{verb} "{key}" ({where})')


def client_state(client: Client) -> ClientState:
    if client.kind == "claude-code":
        return claude_code_state()
    if client.kind == "codex":
        return codex_state()
    return json_client_state(client)


def configure_client(client: Client, uvx: str, state: ClientState) -> ClientResult:
    if client.kind == "json":
        return configure_json_client(client, uvx, state)
    return configure_cli_client(client, uvx, state)


def describe_change(client: Client, uvx: str, state: ClientState) -> str:
    """What configure_client would do, for --dry-run."""
    if client.kind == "json":
        verb = f'update "{state.entry_key}"' if state.status == OUTDATED else f'add "{SERVER_NAME}"'
        return f"would {verb} in {client.config_path}"
    return "would run: " + " && ".join(
        " ".join([Path(c[0]).name, *c[1:]]) for c in _cli_commands(client, uvx, state)
    )


# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"Blender\s+(\d+)\.(\d+)")

# Runs inside Blender. Leaves things alone if the addon is already enabled
# under some module name (e.g. one installed by hand as addon.py), since
# enabling a second copy would clash on class registration.
_ENABLE_SCRIPT = r"""
import json, sys
import addon_utils, bpy
module = sys.argv[sys.argv.index("--") + 1]
names = {"MCP for Blender", "Blender MCP"}
result = {"already": [], "enabled": False, "error": None}
try:
    enabled = {a.module for a in bpy.context.preferences.addons}
    for mod in addon_utils.modules(refresh=True):
        if mod.__name__ in enabled and addon_utils.module_bl_info(mod).get("name") in names:
            result["already"].append(mod.__name__)
    if not result["already"]:
        result["enabled"] = addon_utils.enable(module, default_set=True) is not None
        if result["enabled"]:
            bpy.ops.wm.save_userpref()
        else:
            result["error"] = "Blender couldn't load " + module
except Exception as e:
    result["error"] = str(e)
print("MCPFB_RESULT " + json.dumps(result))
"""


def find_blender_executables(platform: str | None = None, env=None, which=shutil.which) -> list[str]:
    platform = platform or sys.platform
    env = os.environ if env is None else env
    found: list[str] = []
    on_path = which("blender")
    if on_path:
        found.append(on_path)
    if platform == "darwin":
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            found += sorted(glob.glob(str(apps / "Blender*.app" / "Contents" / "MacOS" / "Blender")))
    elif platform == "win32":
        for root in {env.get("ProgramFiles", r"C:\Program Files"), env.get("ProgramFiles(x86)", "")}:
            if root:
                found += sorted(glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")))
        steam = os.path.join(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                             "Steam", "steamapps", "common", "Blender", "blender.exe")
        if os.path.isfile(steam):
            found.append(steam)
    else:
        for path in ("/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender"):
            if os.path.isfile(path):
                found.append(path)
    unique: list[str] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def blender_version(executable: str) -> tuple[int, int] | None:
    try:
        proc = subprocess.run([executable, "--version"], capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(proc.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def newest_blender(executables: list[str]) -> tuple[str, tuple[int, int]] | None:
    versions = [(v, exe) for exe in executables if (v := blender_version(exe))]
    if not versions:
        return None
    version, exe = max(versions)
    return exe, version


def blender_is_running(platform: str | None = None) -> bool:
    platform = platform or sys.platform
    try:
        if platform == "win32":
            # tasklist writes the OEM code page (cp850 on German, cp857 on Turkish Windows), not the
            # ANSI one text=True assumes; its localized "no tasks" message used to crash the decode.
            proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq blender.exe", "/NH"],
                                  capture_output=True, text=True, encoding="oem", errors="replace", timeout=15)
            return "blender.exe" in (proc.stdout or "").lower()
        proc = subprocess.run(["pgrep", "-xi", "blender"], capture_output=True, text=True, timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def addon_module_name(target: Path) -> str:
    """The module name Blender loads an installed addon file under."""
    if target.parent.name == "user_default" and target.parent.parent.name == "extensions":
        return f"bl_ext.user_default.{target.stem}"
    return target.stem


def safe_to_save_prefs(base: Path, version: tuple[int, int]) -> bool:
    """False when saving prefs would cost the user Blender's settings migration.

    The first time a new Blender version opens, it offers to copy settings from
    the previous one, but only if that version has no saved preferences yet.
    Writing them headlessly would silently take that choice away.
    """
    version_dir = base / f"{version[0]}.{version[1]}"
    if (version_dir / "config" / "userpref.blend").is_file():
        return True
    others = [p for p in base.glob("*.*") if p.is_dir() and p.name != version_dir.name
              and re.match(r"^\d+\.\d+$", p.name)] if base.is_dir() else []
    return not others


def enable_addon_headless(executable: str, module: str) -> tuple[bool, str]:
    env = {**os.environ, "BLENDERMCP_NO_UPDATE_CHECK": "1"}
    try:
        proc = subprocess.run(
            [executable, "--background", "--python-expr", _ENABLE_SCRIPT, "--", module],
            capture_output=True, text=True, errors="replace", timeout=180, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Couldn't run Blender: {e}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("MCPFB_RESULT "):
            result = json.loads(line[len("MCPFB_RESULT "):])
            if result.get("already"):
                return True, "Addon was already enabled in Blender"
            if result.get("enabled"):
                return True, "Enabled the addon in Blender"
            return False, result.get("error") or "Blender didn't enable the addon"
    return False, "Blender exited without reporting a result"


@dataclass
class AddonOutcome:
    installed: bool = False
    enabled: bool = False
    lines: list[str] = field(default_factory=list)


_MANUAL_ENABLE = (
    "In Blender: Edit → Preferences → Add-ons → search \"MCP for Blender\" → tick the box."
)


def setup_addon(dry_run: bool) -> AddonOutcome:
    outcome = AddonOutcome()
    executables = find_blender_executables()
    newest = newest_blender(executables) if executables else None
    base = blender_config_base()

    addons_dir = None
    if newest and base and not os.environ.get("BLENDERMCP_ADDONS_DIR"):
        # Install into the folder of the Blender we'll enable it in.
        version_dir = base / f"{newest[1][0]}.{newest[1][1]}"
        candidates = [version_dir / "scripts" / "addons", version_dir / "extensions" / "user_default"]
        existing = find_existing_addon_installs(candidates)
        addons_dir = existing[0].parent if existing else candidates[0]

    if dry_run:
        where = addons_dir or "the newest Blender addons folder"
        outcome.lines.append(f"would install the addon into {where}")
        if newest:
            outcome.lines.append(f"would enable it with {newest[0]} (Blender {newest[1][0]}.{newest[1][1]})")
        else:
            outcome.lines.append("Blender not found: would print the manual enable step")
        return outcome

    result = install_addon(addons_dir)
    if not result.success:
        outcome.lines.append(result.message)
        return outcome
    outcome.installed = True
    outcome.lines.append(f"Installed the addon to {result.target_path}")

    if not newest:
        outcome.lines.append("Couldn't find Blender to enable the addon. " + _MANUAL_ENABLE)
        return outcome
    if blender_is_running():
        outcome.lines.append("Blender is open, so the addon wasn't enabled automatically. "
                             "Quit Blender and run setup again, or: " + _MANUAL_ENABLE)
        return outcome
    if base and not safe_to_save_prefs(base, newest[1]):
        outcome.lines.append(
            f"Blender {newest[1][0]}.{newest[1][1]} hasn't been opened yet. Open it once "
            "(choose whether to import your old settings), then: " + _MANUAL_ENABLE)
        return outcome

    ok, message = enable_addon_headless(newest[0], addon_module_name(Path(result.target_path)))
    outcome.enabled = ok
    outcome.lines.append(message if ok else f"{message}. " + _MANUAL_ENABLE)
    return outcome


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    NEW: "will add",
    OUTDATED: "will update from the old blender-mcp name",
    CONFIGURED: "already set up",
    UNREADABLE: "config isn't valid JSON, skipping",
}


def _parse_selection(answer: str, count: int) -> list[int] | None:
    """Indexes chosen by an answer like '', 'all', 'n', '1 3' or '1,3'."""
    answer = answer.strip().lower()
    if answer in ("", "a", "all", "y", "yes"):
        return list(range(count))
    if answer in ("n", "no", "none"):
        return []
    picked: list[int] = []
    for token in re.split(r"[\s,]+", answer):
        if not token.isdigit() or not 1 <= int(token) <= count:
            return None
        if int(token) - 1 not in picked:
            picked.append(int(token) - 1)
    return picked


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def run_setup(dry_run: bool = False, assume_yes: bool = False, skip_addon: bool = False) -> int:
    print("MCP for Blender setup" + (" (dry run: nothing will be changed)" if dry_run else ""))
    print()

    uvx = find_uvx()
    if not uvx:
        print("Couldn't find uvx. Install uv first: https://docs.astral.sh/uv/getting-started/installation/")
        return 1

    interactive = not assume_yes and sys.stdin is not None and sys.stdin.isatty()

    clients = detect_clients()
    states = [(c, client_state(c)) for c in clients]
    if not states:
        print("No MCP clients found (Claude Desktop, Claude Code, Codex, Cursor, VS Code,")
        print("Devin Desktop/Windsurf, OpenCode, Antigravity). See the README for other clients.")
    else:
        print("Found:")
        width = max(len(c.name) for c, _ in states)
        for c, s in states:
            label = _STATUS_LABEL[s.status]
            if s.status == CONFIGURED and s.detail:
                label += f" ({s.detail})"
            print(f"  {c.name.ljust(width)}  {label}")
        print()

    actionable = [(c, s) for c, s in states if s.status in (NEW, OUTDATED)]
    chosen = actionable
    if actionable and interactive:
        for i, (c, s) in enumerate(actionable, 1):
            print(f"  {i}. {c.name}" + ("  (update)" if s.status == OUTDATED else ""))
        while True:
            picked = _parse_selection(
                _ask("Configure which? [Enter = all, numbers like 1 3, n = none]: "), len(actionable))
            if picked is not None:
                break
            print("  Type numbers from the list, Enter for all, or n for none.")
        chosen = [actionable[i] for i in picked]
        print()

    results: list[ClientResult] = []
    for client, state in chosen:
        if dry_run:
            print(f"  {client.name}: {describe_change(client, uvx, state)}")
            continue
        result = configure_client(client, uvx, state)
        results.append(result)
        print(f"  {'✓' if result.ok else '✗'} {client.name}: {result.message}")
    if chosen:
        print()

    addon = None
    if not skip_addon:
        print("Blender addon:")
        addon = setup_addon(dry_run)
        for line in addon.lines:
            print(f"  {line}")
        print()

    if dry_run:
        return 0

    backups = [r.backup for r in results if r.backup]
    if backups:
        print("Backups of the files changed:")
        for b in backups:
            print(f"  {b}")
        print()

    print("Next:")
    for r in results:
        if r.ok:
            print(f"  • {r.client.restart_hint}")
    if any(r.ok and r.client.kind == "codex" for r in results):
        print("  • Codex: the MCP for Blender plugin adds viewport branding and suggested prompts;")
        print("    see the README. Model generation can outlast Codex's 60s tool timeout; to raise it,")
        print("    add `tool_timeout_sec = 600` under [mcp_servers.blender] in ~/.codex/config.toml.")
    if addon and addon.installed:
        print("  • Open Blender. The addon starts its server on launch (MCP for Blender tab, press N).")
    print("  • Then ask your assistant to build something in Blender.")

    failed = [r for r in results if not r.ok]
    return 1 if failed or (addon is not None and not addon.installed) else 0
