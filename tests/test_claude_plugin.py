"""The Claude plugin pins the server to an exact release, which the directory requires.

Anthropic's directory blocks an unpinned `uvx` launcher, so the plugin names
`mcp-for-blender==<version>`. That pin has to move with every release, along with
the plugin's own `version`, or the directory keeps serving the old server.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "integrations" / "claude" / "mcp-for-blender"


def _package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_plugin_version_matches_package():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == _package_version()


def test_server_is_pinned_to_this_release():
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert servers["blender"]["command"] == "uvx"
    assert servers["blender"]["args"] == [f"mcp-for-blender=={_package_version()}"]


def test_every_pin_in_the_plugin_names_this_release():
    version = _package_version()
    for path in PLUGIN.rglob("*"):
        if path.suffix not in (".json", ".md"):
            continue
        for pinned in re.findall(r"mcp-for-blender==([\w.]+)", path.read_text()):
            assert pinned == version, f"{path.relative_to(ROOT)} pins {pinned}, release is {version}"
