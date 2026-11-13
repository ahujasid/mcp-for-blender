"""The Claude plugin pins the server to an exact release, which the directory requires.

Anthropic's directory doesn't accept a PyPI version as a pin, because a release can
gain new files after it's published. So the plugin ships the release's wheel in
vendor/ and runs it with `uvx --from`. The wheel, the launch command and the
plugin's own `version` have to move with every release, or the directory keeps
serving the old server.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "integrations" / "claude" / "mcp-for-blender"


def _package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _wheel_name() -> str:
    return f"mcp_for_blender-{_package_version()}-py3-none-any.whl"


def test_plugin_version_matches_package():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == _package_version()


def test_vendor_holds_only_this_release():
    assert [p.name for p in (PLUGIN / "vendor").iterdir()] == [_wheel_name()]


def test_server_runs_the_shipped_wheel():
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert servers["blender"]["command"] == "uvx"
    assert servers["blender"]["args"] == [
        "--from", f"${{CLAUDE_PLUGIN_ROOT}}/vendor/{_wheel_name()}", "mcp-for-blender"
    ]


def test_every_version_in_the_readme_names_this_release():
    version = _package_version()
    readme = (PLUGIN / "README.md").read_text()
    for pinned in re.findall(r"mcp[-_]for[-_]blender(?:==|-|/)(\d+\.\d+\.\d+)", readme):
        assert pinned == version, f"README names {pinned}, release is {version}"
