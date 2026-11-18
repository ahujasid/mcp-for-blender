"""Tests for addon install + handshake (no Blender required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from blender_mcp.addon_manager import (
    EXPECTED_ADDON_PROTOCOL_VERSION,
    get_bundled_addon_path,
    handshake_addon,
    install_addon,
    discover_blender_addon_dirs,
)

from conftest import ROOT_ADDON


def test_bundled_addon_exists_and_has_protocol():
    path = get_bundled_addon_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "ADDON_PROTOCOL_VERSION" in text
    assert "get_addon_info" in text


    assert f"ADDON_PROTOCOL_VERSION = {EXPECTED_ADDON_PROTOCOL_VERSION}" in text


def test_root_and_bundled_addon_in_sync():
    root = ROOT_ADDON
    if not root.is_file():
        return
    # Address the bundled copy directly. get_bundled_addon_path() falls back to
    # root addon.py, which would compare the file against itself and pass even
    # when the bundled copy is missing entirely.
    import blender_mcp

    bundled = Path(blender_mcp.__file__).resolve().parent / "bundled" / "addon.py"
    assert bundled.is_file(), (
        "src/blender_mcp/bundled/addon.py is missing — uvx users would ship "
        "without a bundled addon."
    )
    assert root.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8"), (
        "Root addon.py and src/blender_mcp/bundled/addon.py diverged — "
        "copy root → bundled after editing."
    )


def test_root_addon_protocol_matches_server_expectation():
    """ADDON_PROTOCOL_VERSION is hand-synced across two files; catch drift."""
    root = ROOT_ADDON
    if not root.is_file():
        return
    import re

    match = re.search(
        r"ADDON_PROTOCOL_VERSION\s*=\s*(\d+)", root.read_text(encoding="utf-8")
    )
    assert match is not None, "addon.py is missing ADDON_PROTOCOL_VERSION"
    assert int(match.group(1)) == EXPECTED_ADDON_PROTOCOL_VERSION, (
        "addon.py ADDON_PROTOCOL_VERSION and addon_manager."
        "EXPECTED_ADDON_PROTOCOL_VERSION diverged."
    )


def test_install_addon_copies_into_target_dir(tmp_path: Path):
    addons = tmp_path / "scripts" / "addons"
    # Pre-existing oddly named install (what many users have)
    old = addons
    addons.mkdir(parents=True)
    legacy = addons / "addon.py"
    legacy.write_text('bl_info = {\n    "name": "Blender MCP"\n}\n# old\n', encoding="utf-8")

    result = install_addon(addons)
    assert result.success is True
    assert result.target_path is not None
    installed = Path(result.target_path)
    assert installed.is_file()
    assert "ADDON_PROTOCOL_VERSION" in installed.read_text(encoding="utf-8")
    # Legacy file should also have been overwritten
    assert "ADDON_PROTOCOL_VERSION" in legacy.read_text(encoding="utf-8")


def test_handshake_up_to_date():
    blender = MagicMock()
    blender.send_command.return_value = {
        "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": [1, 3],
        "capabilities": ["get_addon_info", "get_world_state_snapshot"],
        "blender_version": "4.2.0",
    }
    result = handshake_addon(blender)
    assert result.up_to_date is True
    assert result.source == "native"
    assert result.warning is None


def test_handshake_missing_command_on_old_addon():
    blender = MagicMock()
    blender.send_command.side_effect = Exception(
        "Unknown command type: get_addon_info"
    )
    result = handshake_addon(blender)
    assert result.up_to_date is False
    assert result.source == "missing"
    assert "install-addon" in (result.warning or "").lower() or "restart" in (result.warning or "").lower()


def test_handshake_outdated_protocol():
    blender = MagicMock()
    blender.send_command.return_value = {
        "protocol_version": 1,
        "addon_version": [1, 2],
        "capabilities": [],
        "blender_version": "4.0.0",
    }
    result = handshake_addon(blender)
    assert result.up_to_date is False
    assert result.protocol_version == 1


def _stale_addon_source() -> str:
    from blender_mcp import addon_manager as am

    # Derive the stale marker from the current expected version so this helper
    # keeps producing a genuinely outdated file across protocol bumps.
    return am.get_bundled_addon_path().read_text(encoding="utf-8").replace(
        f"ADDON_PROTOCOL_VERSION = {am.EXPECTED_ADDON_PROTOCOL_VERSION}",
        "ADDON_PROTOCOL_VERSION = 0",
        1,
    )


def test_startup_check_never_writes(tmp_path: Path, monkeypatch):
    """Starting the server must not modify the user's Blender files."""
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    stale = addons / "blender_mcp.py"
    stale.write_text(_stale_addon_source(), encoding="utf-8")
    before = stale.read_bytes()
    listing_before = sorted(p.name for p in addons.iterdir())

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()

    assert report.needs_action is True
    assert str(stale) in report.outdated_paths
    assert "install-addon" in report.message
    # The whole point of detect-and-tell: nothing on disk changed.
    assert stale.read_bytes() == before
    assert sorted(p.name for p in addons.iterdir()) == listing_before


def test_startup_check_reports_current(tmp_path: Path, monkeypatch):
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    (addons / "blender_mcp.py").write_text(
        am.get_bundled_addon_path().read_text(encoding="utf-8"), encoding="utf-8"
    )

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()
    assert report.needs_action is False
    assert report.reason == "already_current"


def test_startup_check_reports_missing_install(tmp_path: Path, monkeypatch):
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()
    assert report.missing is True
    assert report.needs_action is True
    assert "install-addon" in report.message


def test_install_updates_extensions_dir_when_addon_lives_there(
    tmp_path: Path, monkeypatch
):
    """Blender 4.2+: update the loaded copy, don't add a second one."""
    from blender_mcp import addon_manager as am

    scripts = tmp_path / "4.2" / "scripts" / "addons"
    extensions = tmp_path / "4.2" / "extensions" / "user_default"
    scripts.mkdir(parents=True)
    extensions.mkdir(parents=True)
    installed = extensions / "blender_mcp.py"
    installed.write_text(_stale_addon_source(), encoding="utf-8")

    # discover_blender_addon_dirs lists scripts/addons first.
    monkeypatch.setattr(
        am, "discover_blender_addon_dirs", lambda: [scripts, extensions]
    )
    result = am.install_addon()

    assert result.success is True
    assert am.read_addon_protocol_version(installed) == (
        am.EXPECTED_ADDON_PROTOCOL_VERSION
    ), "the actually-loaded extensions copy was left stale"
    assert not (scripts / "blender_mcp.py").exists(), (
        "installed a duplicate into scripts/addons instead of updating in place"
    )


def test_repeat_install_preserves_original_backup(tmp_path: Path):
    """A second install must not overwrite the .bak holding the user's edits."""
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    target = addons / "blender_mcp.py"
    original = _stale_addon_source() + "\n# USER LOCAL EDIT\n"
    target.write_text(original, encoding="utf-8")

    assert am.install_addon(addons).success
    backup = target.with_suffix(".py.bak")
    assert backup.is_file()
    assert "USER LOCAL EDIT" in backup.read_text(encoding="utf-8")

    # Second run: file already matches the bundled source, so nothing to back up.
    assert am.install_addon(addons).success
    assert "USER LOCAL EDIT" in backup.read_text(encoding="utf-8"), (
        "repeat install clobbered the backup of the user's previous addon"
    )


def test_install_targets_newest_blender_after_upgrade(tmp_path: Path, monkeypatch):
    """#431: Blender 5.3 doesn't load 5.2's add-ons, so install there, not into 5.2."""
    from blender_mcp import addon_manager as am

    old = tmp_path / "5.2" / "scripts" / "addons"
    new = tmp_path / "5.3" / "scripts" / "addons"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "blender_mcp.py").write_text(_stale_addon_source(), encoding="utf-8")

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [new, old])
    result = am.install_addon()

    assert result.success is True
    assert result.target_path == str(new / "blender_mcp.py")


def test_startup_check_flags_addon_missing_from_newest_blender(tmp_path: Path, monkeypatch):
    from blender_mcp import addon_manager as am

    old = tmp_path / "5.2" / "scripts" / "addons"
    new = tmp_path / "5.3" / "scripts" / "addons"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "blender_mcp.py").write_text(
        am.get_bundled_addon_path().read_text(encoding="utf-8"), encoding="utf-8"
    )

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [new, old])
    report = am.check_addon_status_on_startup()

    assert report.needs_action is True
    assert report.reason == "not_in_newest_blender"
    assert "Blender 5.2" in report.message and "Blender 5.3" in report.message
    assert "install-addon" in report.message


def test_discover_finds_flatpak_and_orders_by_version(tmp_path: Path, monkeypatch):
    """Flatpak keeps its config under ~/.var/app; 5.10 is newer than 5.9."""
    from blender_mcp import addon_manager as am

    native = tmp_path / ".config" / "blender"
    flatpak = tmp_path / ".var" / "app" / "org.blender.Blender" / "config" / "blender"
    for version_dir in (native / "5.9", flatpak / "5.10", flatpak / "5.2"):
        version_dir.mkdir(parents=True)

    monkeypatch.setattr(am.sys, "platform", "linux")
    monkeypatch.setattr(am.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("BLENDER_USER_ADDONS", raising=False)
    monkeypatch.delenv("BLENDERMCP_ADDONS_DIR", raising=False)

    assert am.discover_blender_addon_dirs() == [
        flatpak / "5.10" / "scripts" / "addons",
        native / "5.9" / "scripts" / "addons",
        flatpak / "5.2" / "scripts" / "addons",
    ]
