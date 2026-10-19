"""The addon's self-updater, which pulls addon.py from the repo's main branch.

What matters: it never offers a downgrade, never installs something that isn't
the addon, and leaves the installed file untouched when anything goes wrong.
"""
import types

import pytest

from conftest import ROOT_ADDON
from test_polypizza import FakeResponse, _load_addon, _scene

SOURCE = ROOT_ADDON.read_text(encoding="utf-8")


def _with_version(version, protocol):
    """The real addon.py with its version and protocol swapped."""
    import re

    text = re.sub(r'"version": \(\d+, \d+\)', f'"version": {version}', SOURCE, count=1)
    return re.sub(r"^ADDON_PROTOCOL_VERSION = \d+", f"ADDON_PROTOCOL_VERSION = {protocol}", text, count=1, flags=re.M)


@pytest.fixture
def addon(monkeypatch):
    module = _load_addon(monkeypatch, _scene())
    module._addon_update.update(status="idle", version=None, source=None, error=None)
    return module


def _serve(monkeypatch, addon, response):
    monkeypatch.setattr(addon.requests, "get", lambda url, timeout=None: response, raising=False)


def test_release_key_reads_version_and_protocol(addon):
    key = addon.addon_release_key(SOURCE)
    assert key == tuple(addon.bl_info["version"]) + (0, addon.ADDON_PROTOCOL_VERSION)
    assert addon.addon_release_key(_with_version((2, 0, 3), 40)) == (2, 0, 3, 40)
    assert addon.addon_version_label((2, 0, 3, 40)) == "2.0.3"
    assert addon.addon_version_label((1, 8, 0, 12)) == "1.8"


@pytest.mark.parametrize("text", [
    "print('hello')",
    SOURCE.replace('"name": "MCP for Blender"', '"name": "Something Else"', 1),
    SOURCE.replace("ADDON_PROTOCOL_VERSION = ", "PROTOCOL = ", 1),
], ids=["not-the-addon", "renamed-addon", "no-protocol"])
def test_release_key_rejects_files_that_are_not_the_addon(addon, text):
    assert addon.addon_release_key(text) is None


def test_newer_main_is_offered(monkeypatch, addon):
    major, minor = addon.bl_info["version"][:2]
    newer = _with_version((major, minor + 1), addon.ADDON_PROTOCOL_VERSION)
    _serve(monkeypatch, addon, types.SimpleNamespace(status_code=200, text=newer))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "available"
    assert addon._addon_update["version"][:2] == (major, minor + 1)
    assert addon._addon_update["source"] == newer


@pytest.mark.parametrize("delta", [0, -1])
def test_same_or_older_main_is_never_offered(monkeypatch, addon, delta):
    major, minor = addon.bl_info["version"][:2]
    _serve(monkeypatch, addon, types.SimpleNamespace(
        status_code=200, text=_with_version((major, minor + delta), addon.ADDON_PROTOCOL_VERSION)))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "current"
    assert addon._addon_update["source"] is None


def test_a_protocol_bump_alone_counts_as_newer(monkeypatch, addon):
    _serve(monkeypatch, addon, types.SimpleNamespace(
        status_code=200, text=_with_version(tuple(addon.bl_info["version"]), addon.ADDON_PROTOCOL_VERSION + 1)))
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "available"


@pytest.mark.parametrize("response", [
    FakeResponse(status_code=404),
    types.SimpleNamespace(status_code=200, text="<html>rate limited</html>"),
    types.SimpleNamespace(status_code=200, text=_with_version((99, 0), 99) + "\ndef broken(:\n"),
])
def test_bad_downloads_are_errors_not_updates(monkeypatch, addon, response):
    _serve(monkeypatch, addon, response)
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "error"
    assert addon._addon_update["source"] is None


def test_offline_is_an_error_not_a_crash(monkeypatch, addon):
    def offline(url, timeout=None):
        raise ConnectionError("no network")
    monkeypatch.setattr(addon.requests, "get", offline, raising=False)
    addon.check_for_addon_update()
    assert addon._addon_update["status"] == "error"
    assert "no network" in addon._addon_update["error"]


def test_install_replaces_the_file_and_keeps_a_backup(tmp_path, addon):
    target = tmp_path / "addon.py"
    target.write_text(SOURCE, encoding="utf-8")
    newer = _with_version((99, 0), 99)
    addon.install_addon_update(str(target), newer)
    assert target.read_text(encoding="utf-8") == newer
    assert (tmp_path / "addon.py.bak").read_text(encoding="utf-8") == SOURCE
    assert not (tmp_path / "addon.py.new").exists()


@pytest.mark.parametrize(
    "bad",
    ["print('not the addon')", _with_version((99, 0), 99) + "\ndef broken(:\n"],
    ids=["not-the-addon", "syntax-error"],
)
def test_install_refuses_anything_but_a_valid_addon(tmp_path, addon, bad):
    target = tmp_path / "addon.py"
    target.write_text(SOURCE, encoding="utf-8")
    with pytest.raises(Exception):
        addon.install_addon_update(str(target), bad)
    assert target.read_text(encoding="utf-8") == SOURCE
    assert list(tmp_path.iterdir()) == [target]
