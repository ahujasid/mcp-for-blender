"""The version sent with every telemetry event is never null."""
from importlib.metadata import PackageNotFoundError

from blender_mcp import telemetry


def test_metadata_without_a_version_reports_unknown(monkeypatch):
    # A dist-info folder left without METADATA makes version() return None.
    monkeypatch.setattr(telemetry, "version", lambda name: None)
    assert telemetry.get_package_version() == "unknown"


def test_pre_rename_name_is_only_a_fallback_for_a_missing_package(monkeypatch):
    def version(name):
        if name == "mcp-for-blender":
            raise PackageNotFoundError(name)
        return "1.9.4"
    monkeypatch.setattr(telemetry, "version", version)
    assert telemetry.get_package_version() == "1.9.4"
