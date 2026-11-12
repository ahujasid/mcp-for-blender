"""
`mcp-for-blender update`: bring an existing install's server and addon up to date.

MCP clients launch the server as `uvx mcp-for-blender`, which keeps running
whatever version uv cached (or installed with `uv tool install`). So when PyPI
has something newer, this upgrades any uv tool install and re-runs itself as
`uvx --refresh-package mcp-for-blender mcp-for-blender@<latest> update`. That
refreshes uv's cache for the clients' next launch, and the re-run installs the
addon bundled with the new release rather than the one we are running.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .addon_manager import addon_version_label, update_installed_addons
from .setup_cli import OUTDATED, PACKAGE, blender_is_running, client_state, detect_clients, find_uvx

PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
# Set on the re-run to the version we upgraded from; also stops a re-run loop.
REEXEC_ENV = "BLENDERMCP_UPDATED_FROM"


def version_key(version: str) -> tuple[int, ...]:
    """Comparable key for a release like 2.1.6; pre-release suffixes are ignored."""
    match = re.match(r"\d+(?:\.\d+)*", version.strip())
    return tuple(int(p) for p in match.group(0).split(".")) if match else ()


def current_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def fetch_latest_version(timeout: float = 10.0) -> tuple[str | None, str]:
    """(latest version on PyPI, error message if it couldn't be read)."""
    import httpx

    # server.py configures INFO logging at import; keep the request line out of the output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        response = httpx.get(PYPI_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response.json()["info"]["version"], ""
    except Exception as e:
        return None, str(e) or type(e).__name__


def find_uv(uvx: str | None) -> str | None:
    """uv itself, preferring the one installed beside the uvx we found."""
    if uvx:
        sibling = Path(uvx).with_name("uv.exe" if sys.platform == "win32" else "uv")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("uv")


def uv_tool_installed(uv: str) -> bool:
    """True when `uv tool install mcp-for-blender` was used; uvx then prefers that copy."""
    try:
        proc = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[:1] == [PACKAGE] for line in (proc.stdout or "").splitlines())


def _update_server(uvx: str, latest: str, dry_run: bool) -> int:
    """Upgrade to `latest` and re-run update from it. Returns the re-run's exit code."""
    uv = find_uv(uvx)
    if uv and uv_tool_installed(uv):
        if dry_run:
            print(f"  would run: uv tool upgrade {PACKAGE}")
        else:
            proc = subprocess.run([uv, "tool", "upgrade", PACKAGE])
            if proc.returncode != 0:
                print(f"  `uv tool upgrade {PACKAGE}` failed; see the output above.")
                return 1

    command = [uvx, "--refresh-package", PACKAGE, f"{PACKAGE}@{latest}", "update"]
    if dry_run:
        command.append("--dry-run")
    current = current_version() or "unknown"
    try:
        proc = subprocess.run(command, env={**os.environ, REEXEC_ENV: current})
    except OSError as e:
        print(f"  Couldn't run uvx: {e}")
        return 1
    return proc.returncode


def _update_addon(dry_run: bool) -> tuple[bool, bool]:
    """Print what happened to each installed addon. Returns (any updated, any failed)."""
    print("Blender addon:")
    try:
        results = update_installed_addons(dry_run=dry_run)
    except FileNotFoundError as e:
        print(f"  {e}")
        return False, True
    if not results:
        print(f"  Not installed. Run `uvx {PACKAGE} setup` to install and enable it.")
        return False, False

    verb = "would update" if dry_run else "updated"
    for r in results:
        if r.action == "updated":
            print(f"  ✓ {verb} {r.path} (was {addon_version_label(r.installed)})")
        elif r.action == "current":
            print(f"  {r.path}: already up to date")
        elif r.action == "newer":
            print(f"  {r.path}: {addon_version_label(r.installed)} is newer than this release, left alone")
        else:
            print(f"  ✗ {r.path}: {r.detail}")
    updated = any(r.action == "updated" for r in results)
    if updated and not dry_run:
        print("  (the previous file is kept beside each one as .bak)")
    return updated, any(r.action == "failed" for r in results)


def _old_package_clients() -> list[str]:
    """Clients still configured to run the pre-rename blender-mcp package."""
    names: list[str] = []
    for client in detect_clients():
        try:
            if client_state(client).status == OUTDATED:
                names.append(client.name)
        except Exception:
            continue
    return names


def run_update(dry_run: bool = False) -> int:
    updated_from = os.environ.get(REEXEC_ENV)
    current = current_version()

    if updated_from is None:
        print("MCP for Blender update" + (" (dry run: nothing will be changed)" if dry_run else ""))
        print()

    server_updated = False
    if updated_from is not None:
        server_updated = updated_from != current
        verb = "would update" if dry_run else "updated"
        print(f"Server: {verb} {updated_from} → {current}")
    else:
        latest, error = fetch_latest_version()
        if latest is None:
            print(f"Server: {current or 'unknown version'} (couldn't check PyPI for a newer one: {error})")
        elif current and version_key(current) >= version_key(latest):
            print(f"Server: {current}, the latest release")
        else:
            print(f"Server: {current or 'unknown version'} → {latest}")
            uvx = find_uvx()
            if not uvx:
                print(f"  uvx not found. If you installed with pip, run: pip install -U {PACKAGE}")
                print(f"  then run `{PACKAGE} update` again to update the addon.")
                return 1
            return _update_server(uvx, latest, dry_run)
    print()

    addon_updated, addon_failed = _update_addon(dry_run)
    print()

    old_clients = _old_package_clients()
    if old_clients:
        print(f"Still running the old blender-mcp package: {', '.join(old_clients)}.")
        print(f"  Run `uvx {PACKAGE} setup` to switch them over.")
        print()

    if dry_run:
        return 0

    print("Next:")
    if server_updated:
        print("  • Restart your MCP clients (Claude Desktop, Cursor, ...) so they start the new server.")
    if addon_updated:
        if blender_is_running():
            print("  • Blender is open: restart it, or Preferences → Add-ons → disable and re-enable")
            print("    'Interface: MCP for Blender', to load the new addon.")
        else:
            print("  • Open Blender. The new addon loads on launch.")
    if not server_updated and not addon_updated:
        print("  • Nothing to do: everything is up to date.")
    return 1 if addon_failed else 0
