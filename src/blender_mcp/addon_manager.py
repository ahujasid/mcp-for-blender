"""
Bundle, install, and version-check the MCP for Blender addon.

Existing users often update only the MCP server (`uvx blender-mcp`). This module:
1. Ships a bundled copy of addon.py inside the package
2. Can copy it into Blender's user addons directory (`install-addon`)
3. Handshake with a running addon to detect outdated installs
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("BlenderMCPServer")

# Must match ADDON_PROTOCOL_VERSION in addon.py / bundled/addon.py
EXPECTED_ADDON_PROTOCOL_VERSION = 6

_ADDON_MARKER = 'bl_info = {\n    "name": "MCP for Blender"'
_INSTALLED_FILENAME = "blender_mcp.py"
_PROTOCOL_RE = re.compile(r"ADDON_PROTOCOL_VERSION\s*=\s*(\d+)")
# Matches the current name and the pre-rename "Blender MCP" so install-addon
# still replaces installs from older releases.
_BL_INFO_NAME_RE = re.compile(
    r"""["']name["']\s*:\s*["'](?:MCP for Blender|Blender MCP)["']"""
)


def read_addon_protocol_version(path: Path) -> int | None:
    """Parse ADDON_PROTOCOL_VERSION from an installed addon file."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _PROTOCOL_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def addon_file_needs_update(path: Path) -> bool:
    """True if path is missing protocol metadata or behind the bundled addon."""
    if not path.is_file():
        return True
    installed = read_addon_protocol_version(path)
    if installed is None:
        return True
    return installed < EXPECTED_ADDON_PROTOCOL_VERSION


@dataclass
class AddonStatusReport:
    """Read-only view of the addon files on disk versus the bundled copy."""

    checked: bool
    outdated_paths: list[str]
    missing: bool
    message: str
    reason: str | None = None

    @property
    def needs_action(self) -> bool:
        return bool(self.outdated_paths) or self.missing


_UPDATE_HINT = (
    "Run `uvx blender-mcp install-addon` to update it, then in Blender: "
    "Preferences → Add-ons → disable and re-enable 'Interface: MCP for Blender' "
    "(or restart Blender) and click Start MCP Server."
)


def check_addon_status_on_startup() -> AddonStatusReport:
    """
    Report whether the on-disk Blender addon is behind the bundled copy.

    Deliberately read-only. Starting an MCP server is not a request to modify
    files in the user's Blender configuration, and a silent overwrite at an
    unrelated moment can discard local edits with no prompt and no undo. We
    detect and tell; `install-addon` does the writing, when the user asks.
    Never raises; safe to call from server lifespan.
    """
    try:
        dirs = discover_blender_addon_dirs()
        if not dirs:
            return AddonStatusReport(
                checked=False,
                outdated_paths=[],
                missing=False,
                reason="no_addons_dir",
                message=(
                    "Could not find a Blender addons folder. If Blender is "
                    "installed, set BLENDERMCP_ADDONS_DIR, or install addon.py "
                    "manually from the repo."
                ),
            )

        existing = find_existing_addon_installs(dirs)
        if not existing:
            return AddonStatusReport(
                checked=True,
                outdated_paths=[],
                missing=True,
                reason="not_installed",
                message=(
                    "MCP for Blender addon not found in any Blender addons folder. "
                    "Run `uvx blender-mcp install-addon` to install it."
                ),
            )

        outdated = [str(p) for p in existing if addon_file_needs_update(p)]
        if not outdated:
            return AddonStatusReport(
                checked=True,
                outdated_paths=[],
                missing=False,
                reason="already_current",
                message=(
                    f"Blender addon on disk is current "
                    f"(protocol {EXPECTED_ADDON_PROTOCOL_VERSION})."
                ),
            )

        return AddonStatusReport(
            checked=True,
            outdated_paths=outdated,
            missing=False,
            reason="outdated",
            message=(
                f"MCP for Blender addon on disk is outdated (expected protocol "
                f"{EXPECTED_ADDON_PROTOCOL_VERSION}): {', '.join(outdated)}. "
                + _UPDATE_HINT
            ),
        )
    except Exception as e:
        logger.debug(f"Addon status check failed: {e}")
        return AddonStatusReport(
            checked=False,
            outdated_paths=[],
            missing=False,
            reason="error",
            message=f"Could not check Blender addon status: {e}",
        )


@dataclass
class AddonInstallResult:
    success: bool
    message: str
    target_path: str | None = None
    addons_dir: str | None = None


@dataclass
class AddonHandshake:
    up_to_date: bool
    protocol_version: int | None
    addon_version: list[int] | None
    capabilities: list[str]
    blender_version: str | None
    source: str  # native | missing | error
    warning: str | None = None


def get_bundled_addon_path() -> Path:
    """Resolve the addon.py shipped with this package (or repo root in editable installs)."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "bundled" / "addon.py",
        here.parents[1] / "addon.py",  # repo root when running from src layout
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Bundled MCP for Blender addon.py not found. Reinstall blender-mcp or "
        "copy addon.py from the GitHub repo into Blender manually."
    )


def discover_blender_addon_dirs() -> list[Path]:
    """Find Blender user scripts/addons directories across versions."""
    dirs: list[Path] = []
    home = Path.home()

    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Blender"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "Blender Foundation" / "Blender" if appdata else None
    else:
        base = home / ".config" / "blender"

    if base and base.is_dir():
        for child in sorted(base.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            # Blender versions look like 3.6, 4.0, 4.2
            if not re.match(r"^\d+\.\d+", child.name):
                continue
            dirs.append(child / "scripts" / "addons")
            # Blender 4.2+ installs through the extensions system.
            extensions = child / "extensions" / "user_default"
            if extensions.is_dir():
                dirs.append(extensions)

    env = os.environ.get("BLENDER_USER_ADDONS") or os.environ.get("BLENDERMCP_ADDONS_DIR")
    if env:
        env_path = Path(env).expanduser()
        dirs.insert(0, env_path)

    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _is_blendermcp_addon_file(path: Path) -> bool:
    """True only for a file whose bl_info declares it as the MCP for Blender addon.

    Deliberately narrower than a substring search for "BlenderMCPServer": that
    also matches a user's own fork or a script that merely references the class,
    and install_addon overwrites everything this returns True for.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _BL_INFO_NAME_RE.search(text) is not None


def find_existing_addon_installs(addons_dirs: list[Path] | None = None) -> list[Path]:
    """Locate already-installed MCP for Blender addon files."""
    found: list[Path] = []
    for addons_dir in addons_dirs or discover_blender_addon_dirs():
        if not addons_dir.is_dir():
            continue
        for path in addons_dir.iterdir():
            if path.is_file() and path.suffix == ".py" and _is_blendermcp_addon_file(path):
                found.append(path)
            elif path.is_dir() and (path / "__init__.py").is_file():
                init = path / "__init__.py"
                if _is_blendermcp_addon_file(init):
                    found.append(init)
    return found


def _backup_addon_file(path: Path, source: Path | None = None) -> Path | None:
    """Keep one .bak copy before overwriting, so local edits are recoverable.

    Skipped when the file already matches what we are about to write: a repeat
    install would otherwise overwrite a .bak holding the user's real previous
    version with an identical copy of the bundled addon, destroying the very
    edits the backup exists to preserve.
    """
    if not path.is_file():
        return None
    if source is not None:
        try:
            if path.read_bytes() == source.read_bytes():
                return None
        except OSError as e:
            logger.debug(f"Could not compare {path} with {source}: {e}")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError as e:
        logger.debug(f"Could not back up {path}: {e}")
        return None


def install_addon(
    addons_dir: Path | None = None,
    *,
    create_dir: bool = True,
) -> AddonInstallResult:
    """
    Copy the bundled addon into Blender's user addons folder.

    Replaces known existing MCP for Blender addon files in that directory.
    User must disable/enable the addon or restart Blender to load the new code.
    """
    try:
        source = get_bundled_addon_path()
    except FileNotFoundError as e:
        return AddonInstallResult(False, str(e))

    if addons_dir is None:
        dirs = discover_blender_addon_dirs()
        if not dirs:
            return AddonInstallResult(
                False,
                "Could not find a Blender user addons directory. "
                "Set BLENDERMCP_ADDONS_DIR to your Blender scripts/addons path, "
                "or install addon.py manually from the repo.",
            )
        # Update where the addon already lives rather than the newest
        # scripts/addons dir.
        existing = find_existing_addon_installs(dirs)
        addons_dir = existing[0].parent if existing else dirs[0]

    addons_dir = Path(addons_dir).expanduser()
    if not addons_dir.exists():
        if not create_dir:
            return AddonInstallResult(
                False,
                f"Addons directory does not exist: {addons_dir}",
                addons_dir=str(addons_dir),
            )
        try:
            addons_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return AddonInstallResult(
                False,
                f"Failed to create addons directory {addons_dir}: {e}",
                addons_dir=str(addons_dir),
            )

    replaced: list[str] = []
    if addons_dir.is_dir():
        for path in list(addons_dir.iterdir()):
            if path.is_file() and path.suffix == ".py" and _is_blendermcp_addon_file(path):
                _backup_addon_file(path, source)
                shutil.copy2(source, path)
                replaced.append(str(path))

    target = addons_dir / _INSTALLED_FILENAME
    if str(target) not in replaced:
        _backup_addon_file(target, source)
        shutil.copy2(source, target)
        replaced.append(str(target))

    msg = (
        f"Installed MCP for Blender addon to {target}. "
        "In Blender: Preferences → Add-ons → disable then enable "
        "'Interface: MCP for Blender', or restart Blender, then click Start MCP Server."
    )
    if len(replaced) > 1:
        msg += f" Also updated: {', '.join(replaced[:-1])}."

    return AddonInstallResult(
        True,
        msg,
        target_path=str(target),
        addons_dir=str(addons_dir),
    )


def handshake_addon(blender_connection) -> AddonHandshake:
    """
    Query a connected Blender addon for protocol version.

    Old addons without get_addon_info are treated as outdated (but still usable
    via execute_code fallbacks elsewhere).
    """
    try:
        info = blender_connection.send_command("get_addon_info")
        if not isinstance(info, dict):
            return AddonHandshake(
                up_to_date=False,
                protocol_version=None,
                addon_version=None,
                capabilities=[],
                blender_version=None,
                source="error",
                warning="Addon returned invalid get_addon_info payload.",
            )
        protocol = info.get("protocol_version")
        try:
            protocol_i = int(protocol) if protocol is not None else None
        except (TypeError, ValueError):
            protocol_i = None

        up_to_date = (
            protocol_i is not None and protocol_i >= EXPECTED_ADDON_PROTOCOL_VERSION
        )
        warning = None
        if not up_to_date:
            warning = (
                f"Blender addon protocol {protocol_i!r} is behind "
                f"expected {EXPECTED_ADDON_PROTOCOL_VERSION}. "
                "Run `uvx blender-mcp install-addon` to update it, then "
                "restart Blender or disable/enable 'Interface: MCP for Blender', "
                "then Start MCP Server. Trajectory still works via fallbacks."
            )
        return AddonHandshake(
            up_to_date=up_to_date,
            protocol_version=protocol_i,
            addon_version=info.get("addon_version"),
            capabilities=list(info.get("capabilities") or []),
            blender_version=info.get("blender_version"),
            source="native",
            warning=warning,
        )
    except Exception as e:
        msg = str(e).lower()
        if "unknown command" in msg or "get_addon_info" in msg:
            warning = (
                "Blender addon is outdated (no get_addon_info). "
                "Run `uvx blender-mcp install-addon` to update it, then "
                "restart Blender or disable/enable 'Interface: MCP for Blender', "
                "then Start MCP Server. Fallbacks keep working in the meantime."
            )
            return AddonHandshake(
                up_to_date=False,
                protocol_version=None,
                addon_version=None,
                capabilities=[],
                blender_version=None,
                source="missing",
                warning=warning,
            )
        return AddonHandshake(
            up_to_date=False,
            protocol_version=None,
            addon_version=None,
            capabilities=[],
            blender_version=None,
            source="error",
            warning=f"Addon handshake failed: {e}",
        )


def format_handshake_log(result: AddonHandshake) -> str:
    if result.up_to_date:
        return (
            f"Blender addon up to date "
            f"(protocol {result.protocol_version}, "
            f"addon {result.addon_version}, "
            f"Blender {result.blender_version})"
        )
    return result.warning or "Blender addon may be outdated."


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entry for install-addon / addon-status. Returns process exit code."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="blender-mcp",
        description="MCP for Blender server and addon installer",
    )
    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser(
        "install-addon",
        help="Copy the bundled addon.py into Blender's user addons folder",
    )
    install_p.add_argument(
        "--addons-dir",
        type=str,
        default=None,
        help="Override Blender scripts/addons directory "
        "(or set BLENDERMCP_ADDONS_DIR)",
    )

    sub.add_parser(
        "addon-paths",
        help="List discovered Blender user addons directories",
    )

    args = parser.parse_args(argv)

    if args.command == "install-addon":
        result = install_addon(
            Path(args.addons_dir) if args.addons_dir else None,
        )
        print(result.message)
        return 0 if result.success else 1

    if args.command == "addon-paths":
        dirs = discover_blender_addon_dirs()
        if not dirs:
            print("No Blender addons directories found.")
            return 1
        for d in dirs:
            marker = " (exists)" if d.is_dir() else " (missing)"
            print(f"{d}{marker}")
        existing = find_existing_addon_installs(dirs)
        if existing:
            print("\nExisting MCP for Blender installs:")
            for p in existing:
                print(f"  {p}")
        return 0

    # No subcommand → caller should start MCP server
    return -1
