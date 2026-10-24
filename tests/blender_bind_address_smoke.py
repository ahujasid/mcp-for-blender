"""Check real Blender RNA registration and preference persistence.

Use an EMPTY, disposable configuration directory, never your usual profile:

    BLENDER_USER_CONFIG=/tmp/blendermcp-bind-test blender --background \
        --factory-startup --python-exit-code 1 \
        --python tests/blender_bind_address_smoke.py -- write
    BLENDER_USER_CONFIG=/tmp/blendermcp-bind-test blender --background \
        --python-exit-code 1 --python tests/blender_bind_address_smoke.py -- read

Run both commands from the repository root. They deliberately do not start a
network listener. GUI startup and connectivity between machines need separate
manual verification; Blender refuses to start this add-on's server in background
mode. This script is not part of the regular pytest suite.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import bpy


def main():
    if not os.environ.get("BLENDER_USER_CONFIG"):
        raise RuntimeError("Set BLENDER_USER_CONFIG to an empty, disposable directory")
    args = sys.argv[sys.argv.index("--") + 1 :]
    if args not in (["write"], ["read"]):
        raise ValueError("Expected -- write or -- read")
    phase = args[0]
    addon_name = "blendermcp_bind_smoke"
    source = Path(__file__).resolve().parents[1] / "addon.py"
    config = Path(os.environ["BLENDER_USER_CONFIG"])
    if phase == "write" and (config / "userpref.blend").exists():
        raise RuntimeError("The write phase requires an empty test profile")

    # Prevent update checks. The addon never starts a server in background mode.
    os.environ["BLENDERMCP_NO_UPDATE_CHECK"] = "1"
    spec = importlib.util.spec_from_file_location(addon_name, source)
    addon = importlib.util.module_from_spec(spec)
    sys.modules[addon_name] = addon
    spec.loader.exec_module(addon)
    addon.register()
    try:
        entry = bpy.context.preferences.addons.get(addon_name)
        if phase == "read":
            assert entry is not None, "Saved add-on preferences disappeared on restart"
        if entry is None:
            entry = bpy.context.preferences.addons.new()
            entry.module = addon_name
        prefs = entry.preferences
        assert prefs is not None, "Blender did not register AddonPreferences"

        if phase == "write":
            assert prefs.bind_address == "localhost"
            assert prefs.allow_remote_connections is False
            prefs.bind_address = "127.0.0.2"
            prefs.allow_remote_connections = True
            config.mkdir(parents=True, exist_ok=True)
            assert bpy.ops.wm.save_userpref() == {"FINISHED"}
            assert (config / "userpref.blend").is_file()
            checks = ["RNA defaults", "preference save"]
        else:
            assert prefs.bind_address == "127.0.0.2"
            assert prefs.allow_remote_connections is True
            assert addon._blendermcp_bind_address() == "127.0.0.2"
            prefs.bind_address = "0.0.0.0"
            prefs.allow_remote_connections = False
            try:
                addon._blendermcp_bind_address()
            except ValueError as exc:
                assert "Allow Remote Connections" in str(exc)
            else:
                raise AssertionError("Wildcard binding was accepted without opt-in")
            prefs.allow_remote_connections = True
            assert addon._blendermcp_bind_address() == "0.0.0.0"
            checks = ["preferences survive restart", "loopback", "remote opt-in"]
        print(
            "BIND_ADDRESS_SMOKE "
            + json.dumps(
                {
                    "phase": phase,
                    "blender": bpy.app.version_string,
                    "checks": checks,
                    "passed": True,
                }
            )
        )
    finally:
        addon.unregister()


if __name__ == "__main__":
    main()
