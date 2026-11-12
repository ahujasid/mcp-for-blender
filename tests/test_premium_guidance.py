"""Premium tells the agent which generators the user's plan includes."""
import types

from blender_mcp import server
from blender_mcp.premium_hint import premium_generation_guidance
from test_sketchfab_status import _load_addon


def test_no_guidance_without_premium_generators():
    assert premium_generation_guidance([]) == ""
    assert premium_generation_guidance(None) == ""
    assert premium_generation_guidance(["unknown"]) == ""


def test_guidance_names_only_the_enabled_generators():
    text = premium_generation_guidance(["tripo", "hunyuan3d"])
    assert "Tripo" in text and "Hunyuan3D" in text
    assert "generate_3d" in text
    assert "hyper3d" not in text.lower()


class _Blender:
    def __init__(self, reply):
        self.reply = reply

    def send_command(self, cmd, params=None):
        assert cmd == "get_addon_info"
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_premium_generators_are_read_fresh_from_the_addon(monkeypatch):
    monkeypatch.setattr(server, "_addon_handshake", None)
    assert server._premium_generators(_Blender({"premium_generators": ["tripo"]})) == ["tripo"]
    # Older addons don't send the field; a failed lookup must not break generation.
    assert server._premium_generators(_Blender({"protocol_version": 11})) == []
    assert server._premium_generators(_Blender(ConnectionError("gone"))) == []


def _scene(**enabled):
    return types.SimpleNamespace(
        blendermcp_use_hyper3d=enabled.get("hyper3d", False),
        blendermcp_use_hunyuan3d=enabled.get("hunyuan3d", False),
        blendermcp_use_tripo=enabled.get("tripo", False),
    )


def test_addon_reports_enabled_generators_only_in_premium(monkeypatch):
    addon = _load_addon(monkeypatch, _scene(tripo=True, hyper3d=True))
    monkeypatch.setattr(addon, "premium_active", lambda: False)
    assert addon.premium_enabled_generators() == []
    monkeypatch.setattr(addon, "premium_active", lambda: True)
    assert addon.premium_enabled_generators() == ["hyper3d", "tripo"]


def test_premium_lookup_skips_addons_without_get_addon_info(monkeypatch):
    old = types.SimpleNamespace(source="missing")
    monkeypatch.setattr(server, "_addon_handshake", old)

    class _Unreachable:
        def send_command(self, *a, **k):
            raise AssertionError("must not query an addon that lacks get_addon_info")

    assert server._premium_generators(_Unreachable()) == []
