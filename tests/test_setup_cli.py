"""Tests for `mcp-for-blender setup` (no real clients or Blender touched)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blender_mcp import setup_cli
from blender_mcp.setup_cli import (
    CONFIGURED,
    NEW,
    OUTDATED,
    UNREADABLE,
    Client,
    ClientState,
    _parse_selection,
    _strip_jsonc,
    addon_module_name,
    codex_state,
    configure_json_client,
    detect_clients,
    json_client_state,
    safe_to_save_prefs,
    servers_state,
)

UVX = "/abs/bin/uvx"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _client(path: Path, style: str = "mcpServers") -> Client:
    return Client("x", "X", "json", path, style=style)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


# --- merging config files ---------------------------------------------------

def test_adds_entry_and_keeps_other_servers(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    _write(path, {"mcpServers": {"other": {"command": "npx", "args": ["x"]}}, "theme": "dark"})

    result = configure_json_client(_client(path), UVX, ClientState(NEW))

    assert result.ok
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert data["mcpServers"]["blender"] == {"command": UVX, "args": ["mcp-for-blender"]}
    assert data["theme"] == "dark"
    assert result.backup and json.loads(result.backup.read_text())["mcpServers"].keys() == {"other"}


def test_creates_missing_file_without_backup(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    result = configure_json_client(_client(path), UVX, ClientState(NEW))
    assert result.ok and result.backup is None
    assert json.loads(path.read_text()) == {"mcpServers": {"blender": {"command": UVX, "args": ["mcp-for-blender"]}}}


def test_vscode_and_opencode_formats(tmp_path):
    vscode = tmp_path / "mcp.json"
    configure_json_client(_client(vscode, "vscode"), UVX, ClientState(NEW))
    assert json.loads(vscode.read_text()) == {
        "servers": {"blender": {"type": "stdio", "command": UVX, "args": ["mcp-for-blender"]}}
    }

    opencode = tmp_path / "opencode.json"
    configure_json_client(_client(opencode, "opencode"), UVX, ClientState(NEW))
    data = json.loads(opencode.read_text())
    assert data["mcp"]["blender"] == {"type": "local", "command": [UVX, "mcp-for-blender"], "enabled": True}
    assert data["$schema"] == "https://opencode.ai/config.json"


def test_does_not_take_over_an_unrelated_blender_entry(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"blender": {"command": "some-other-blender-tool"}}})
    configure_json_client(_client(path), UVX, ClientState(NEW))
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["blender"] == {"command": "some-other-blender-tool"}
    assert servers["blender-2"]["args"] == ["mcp-for-blender"]


def test_update_replaces_old_package_and_keeps_env(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"blender-mcp": {
        "command": "uvx", "args": ["blender-mcp"], "env": {"BLENDER_PORT": "9877"}}}})
    client = _client(path)

    state = json_client_state(client)
    assert (state.status, state.entry_key) == (OUTDATED, "blender-mcp")
    configure_json_client(client, UVX, state)

    entry = json.loads(path.read_text())["mcpServers"]["blender-mcp"]
    assert entry == {"command": UVX, "args": ["mcp-for-blender"], "env": {"BLENDER_PORT": "9877"}}
    assert json_client_state(client).status == CONFIGURED


def test_jsonc_is_read_and_invalid_json_left_alone(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, '{\n  // mine\n  "servers": {"a": {"command": "x // not a comment"},},\n}\n')
    result = configure_json_client(_client(path, "vscode"), UVX, ClientState(NEW))
    assert result.ok and "comments" in result.message
    assert json.loads(path.read_text())["servers"]["a"] == {"command": "x // not a comment"}

    broken = tmp_path / "broken.json"
    _write(broken, "{not json")
    assert json_client_state(_client(broken)).status == UNREADABLE
    assert not configure_json_client(_client(broken), UVX, ClientState(NEW)).ok
    assert broken.read_text() == "{not json"


def test_strip_jsonc_keeps_strings():
    assert json.loads(_strip_jsonc('{"url": "http://x/*y*/", /* c */ "a": [1,],}')) == {
        "url": "http://x/*y*/", "a": [1]}


def test_servers_state():
    assert servers_state({}).status == NEW
    assert servers_state({"b": {"command": "/x/uvx", "args": ["mcp-for-blender"]}}).status == CONFIGURED
    assert servers_state({"b": {"command": ["uvx", "blender-mcp"]}}).status == OUTDATED
    # pipx installs run the package's own command
    assert servers_state({"b": {"command": "mcp-for-blender"}}).status == CONFIGURED
    assert servers_state({"b": {"command": "uvx", "args": ["blender-mcp-extras"]}}).status == NEW


# --- Codex -------------------------------------------------------------------

def test_codex_state_reads_config_toml(tmp_path):
    env = {"CODEX_HOME": str(tmp_path)}
    assert codex_state(env).status == NEW

    (tmp_path / "config.toml").write_text(
        'model = "x"\n[mcp_servers.blender]\ncommand = "uvx"\nargs = ["blender-mcp"]\n'
        '[mcp_servers.blender.env]\nBLENDER_PORT = "9876"\n'
    )
    state = codex_state(env)
    assert (state.status, state.entry_key) == (OUTDATED, "blender")

    (tmp_path / "config.toml").write_text('[mcp_servers."my blender"]\ncommand = "/x/uvx"\nargs = ["mcp-for-blender"]\n')
    assert codex_state(env).status == CONFIGURED


def test_codex_plugin_counts_as_configured(tmp_path):
    env = {"CODEX_HOME": str(tmp_path)}
    (tmp_path / "plugins" / "cache" / "mcp-for-blender" / "mcp-for-blender").mkdir(parents=True)
    assert codex_state(env).status == CONFIGURED


def test_codex_commands_carry_plugin_env():
    client = Client("codex", "Codex", "codex", cli_path="/bin/codex")
    [add] = setup_cli._cli_commands(client, UVX, ClientState(NEW))
    assert add == ["/bin/codex", "mcp", "add", "blender", "--env", "BLENDER_MCP_APPS=1",
                   "--env", "BLENDER_MCP_OPENAI_FORMS=1", "--", UVX, "mcp-for-blender"]
    remove, add = setup_cli._cli_commands(client, UVX, ClientState(OUTDATED, "blender-mcp"))
    assert remove == ["/bin/codex", "mcp", "remove", "blender-mcp"]
    assert add[3] == "blender-mcp"


def test_claude_code_uses_user_scope():
    client = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    [add] = setup_cli._cli_commands(client, UVX, ClientState(NEW))
    assert add == ["/bin/claude", "mcp", "add", "--scope", "user", "blender", "--", UVX, "mcp-for-blender"]


def test_cli_clients_pick_a_free_name_beside_an_unrelated_blender(tmp_path):
    claude = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    codex = Client("codex", "Codex", "codex", cli_path="/bin/codex")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "CODEX_HOME": str(tmp_path)}
    assert setup_cli._cli_key(claude, ClientState(NEW), env) == "blender"
    assert setup_cli._cli_key(codex, ClientState(NEW), env) == "blender"

    _write(tmp_path / ".claude.json", {"mcpServers": {"blender": {"command": "my-own-blender-tool"}}})
    (tmp_path / "config.toml").write_text('[mcp_servers.blender]\ncommand = "my-own-blender-tool"\n')
    assert setup_cli._cli_key(claude, ClientState(NEW), env) == "blender-2"
    assert setup_cli._cli_key(codex, ClientState(NEW), env) == "blender-2"
    [add] = setup_cli._cli_commands(claude, UVX, ClientState(NEW), "blender-2")
    assert add[5] == "blender-2"

    # Updating an old entry keeps its name.
    assert setup_cli._cli_key(claude, ClientState(OUTDATED, "blender"), env) == "blender"
    assert setup_cli._cli_key(codex, ClientState(OUTDATED, "blender"), env) == "blender"


def test_cli_success_and_dry_run_report_the_name_used(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / ".claude.json", {"mcpServers": {"blender": {"command": "my-own-blender-tool"}}})
    client = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    ran = []
    monkeypatch.setattr(setup_cli, "_run_cli", lambda args: (ran.append(args), (True, ""))[1])

    assert '--scope user blender-2 --' in setup_cli.describe_change(client, UVX, ClientState(NEW))
    result = setup_cli.configure_cli_client(client, UVX, ClientState(NEW))
    assert result.ok and result.key == "blender-2" and '"blender-2"' in result.message
    assert [args[5] for args in ran] == ["blender-2"]


_CODEX_HAND_WRITTEN = {
    "header-comment": '[mcp_servers.blender] # mine\ncommand = "python3"\nargs = ["/opt/mine.py"]\n',
    "inline-table": '[mcp_servers]\nblender = { command = "python3", args = ["/opt/mine.py"] }\n',
    "dotted-keys": 'mcp_servers.blender.command = "python3"\nmcp_servers.blender.args = ["/opt/mine.py"]\n',
}


@pytest.fixture(params=["tomllib", "regex"])
def toml_reader(request, monkeypatch):
    """Run a test with tomllib (3.11+) and with the Python 3.10 regex fallback."""
    if request.param == "regex":
        monkeypatch.setattr(setup_cli, "tomllib", None, raising=False)
    elif sys.version_info < (3, 11):
        pytest.skip("tomllib needs Python 3.11+")
    return request.param


@pytest.mark.parametrize("form", sorted(_CODEX_HAND_WRITTEN))
def test_codex_never_takes_a_hand_written_blender(form, toml_reader, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(_CODEX_HAND_WRITTEN[form])
    client = Client("codex", "Codex", "codex", cli_path="/bin/codex")
    ran = []
    monkeypatch.setattr(setup_cli, "_run_cli", lambda args: (ran.append(args), (True, ""))[1])

    state = codex_state()
    assert state.status == NEW
    assert "codex mcp add blender-2 " in setup_cli.describe_change(client, UVX, state)
    result = setup_cli.configure_cli_client(client, UVX, state)
    assert result.ok and result.key == "blender-2" and '"blender-2"' in result.message
    assert [args[3] for args in ran] == ["blender-2"]


@pytest.mark.parametrize("form", sorted(_CODEX_HAND_WRITTEN))
def test_codex_state_reads_hand_written_installs(form, toml_reader, tmp_path):
    env = {"CODEX_HOME": str(tmp_path)}
    old = _CODEX_HAND_WRITTEN[form].replace('"/opt/mine.py"', '"blender-mcp"')
    (tmp_path / "config.toml").write_text(old)
    state = codex_state(env)
    assert (state.status, state.entry_key) == (OUTDATED, "blender")
    (tmp_path / "config.toml").write_text(old.replace("blender-mcp", "mcp-for-blender"))
    assert codex_state(env).status == CONFIGURED


def test_codex_regex_fallback_ignores_other_tables(monkeypatch):
    monkeypatch.setattr(setup_cli, "tomllib", None, raising=False)
    text = ('model = "x"\nprofile.blender = "y"\n[mcp_servers]\nstartup_timeout = 5\n'
            '"my blender" = { command = "x" }\n[profiles.blender]\nmodel = "y"\n'
            '[mcp_servers.other]\ncommand = "z"\n')
    assert sorted(setup_cli._codex_mcp_sections(text)) == ["my blender", "other"]


def test_json_dry_run_reports_the_free_name(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"blender": {"command": "my-own-blender-tool"}}})
    assert 'add "blender-2"' in setup_cli.describe_change(_client(path), UVX, ClientState(NEW))
    assert 'add "blender"' in setup_cli.describe_change(_client(tmp_path / "none.json"), UVX, ClientState(NEW))


# --- detection ---------------------------------------------------------------

def test_detects_clients_by_config_folders(home):
    (home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
    (home / ".cursor").mkdir()
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".gemini" / "config").mkdir(parents=True)
    which = {"claude": "/bin/claude"}.get

    clients = {c.key: c for c in detect_clients("darwin", {}, which)}

    assert set(clients) == {"claude-desktop", "claude-code", "cursor", "devin", "antigravity"}
    assert clients["devin"].config_path == home / ".codeium" / "windsurf" / "mcp_config.json"
    assert clients["antigravity"].config_path == home / ".gemini" / "config" / "mcp_config.json"


def test_windows_prefers_microsoft_store_claude_folder(home):
    local = home / "AppData" / "Local"
    msix = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    msix.mkdir(parents=True)
    (home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)

    env = {"LOCALAPPDATA": str(local), "APPDATA": str(home / "AppData" / "Roaming")}
    [claude] = [c for c in detect_clients("win32", env, lambda _: None) if c.key == "claude-desktop"]
    assert claude.config_path == msix / "claude_desktop_config.json"


def test_devin_prefers_new_folder(home):
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".config" / "devin").mkdir(parents=True)
    [devin] = [c for c in detect_clients("linux", {}, lambda _: None) if c.key == "devin"]
    assert devin.config_path == home / ".config" / "devin" / "mcp_config.json"


def test_opencode_keeps_existing_jsonc(home):
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.jsonc").write_text("{}")
    [oc] = [c for c in detect_clients("darwin", {}, lambda _: None) if c.key == "opencode"]
    assert oc.config_path.name == "opencode.jsonc"


# --- Blender -----------------------------------------------------------------

def test_addon_module_name():
    assert addon_module_name(Path("/b/4.2/scripts/addons/blender_mcp.py")) == "blender_mcp"
    assert addon_module_name(Path("/b/4.2/extensions/user_default/blender_mcp.py")) == \
        "bl_ext.user_default.blender_mcp"


def test_safe_to_save_prefs_protects_settings_migration(tmp_path):
    # Only version: nothing to migrate from.
    (tmp_path / "4.2").mkdir()
    assert safe_to_save_prefs(tmp_path, (4, 2))
    # A newer version never opened, with an older one to import from: hands off.
    assert not safe_to_save_prefs(tmp_path, (4, 3))
    # Once it has its own prefs, it's fine.
    (tmp_path / "4.3" / "config").mkdir(parents=True)
    (tmp_path / "4.3" / "config" / "userpref.blend").write_bytes(b"")
    assert safe_to_save_prefs(tmp_path, (4, 3))


def test_parse_selection():
    assert _parse_selection("", 3) == [0, 1, 2]
    assert _parse_selection("n", 3) == []
    assert _parse_selection("3, 1 3", 3) == [2, 0]
    assert _parse_selection("4", 3) is None
    assert _parse_selection("x", 3) is None


# --- the command -------------------------------------------------------------

def test_dry_run_changes_nothing(home, monkeypatch, capsys):
    cursor = home / ".cursor" / "mcp.json"
    _write(cursor, {"mcpServers": {"other": {"command": "x"}}})
    before = cursor.read_text()
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: UVX)
    monkeypatch.setattr(setup_cli, "detect_clients", lambda: [Client("cursor", "Cursor", "json", cursor)])

    assert setup_cli.run_setup(dry_run=True, assume_yes=True, skip_addon=True) == 0
    assert cursor.read_text() == before
    assert not cursor.with_name("mcp.json.bak").exists()
    assert "would add" in capsys.readouterr().out


def test_run_setup_configures_and_skips_configured(home, monkeypatch, capsys):
    cursor = home / ".cursor" / "mcp.json"
    vscode = home / "vscode" / "mcp.json"
    _write(vscode, {"servers": {"blender": {"command": "uvx", "args": ["mcp-for-blender"]}}})
    vscode_before = vscode.read_text()
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: UVX)
    monkeypatch.setattr(setup_cli, "detect_clients", lambda: [
        Client("cursor", "Cursor", "json", cursor, restart_hint="Restart Cursor."),
        Client("vscode", "VS Code", "json", vscode, style="vscode"),
    ])

    assert setup_cli.run_setup(assume_yes=True, skip_addon=True) == 0
    assert json.loads(cursor.read_text())["mcpServers"]["blender"]["command"] == UVX
    assert vscode.read_text() == vscode_before
    out = capsys.readouterr().out
    assert "already set up" in out and "Restart Cursor." in out


def test_run_setup_needs_uvx(monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: None)
    assert setup_cli.run_setup(assume_yes=True) == 1
    assert "Install uv" in capsys.readouterr().out


@pytest.mark.parametrize("message, oem", [
    ("INFO: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.", "cp850"),  # German
    ("BİLGİ: Belirtilen ölçütlerle eşleşen çalışan görev yok.", "cp857"),  # Turkish
])
def test_blender_is_running_survives_localized_tasklist(monkeypatch, message, oem):
    # Emulate Windows: tasklist writes the OEM code page, and subprocess decodes with the
    # encoding it was given, else the ANSI code page (cp1252 here), which cannot read byte 0x81.
    def fake_run(args, capture_output, text, timeout, encoding=None, errors="strict"):
        codec = oem if encoding == "oem" else (encoding or "cp1252")
        stdout = message.encode(oem).decode(codec, errors)
        return setup_cli.subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    assert setup_cli.blender_is_running("win32") is False
