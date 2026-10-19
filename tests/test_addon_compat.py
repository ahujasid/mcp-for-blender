"""The MCP server updates itself through uvx, but the addon inside Blender only
changes when the user reinstalls it. New tools and arguments must therefore
degrade cleanly on older addons (protocol 9 shipped up to 2.0.4)."""
import asyncio

import pytest

from blender_mcp import server
from blender_mcp.addon_manager import AddonHandshake


class FakeBlender:
    """An addon with Hunyuan3D on under the user's own key, whose generation finishes at once."""

    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send_command(self, command, params=None, read_only=False):
        if command == "get_telemetry_consent":
            return {"consent": False}
        self.sent.append((command, params))
        if self.error:
            raise Exception(f"Communication error with Blender: {self.error}")
        return {
            "get_addon_info": {"premium_generators": []},
            "get_hunyuan3d_status": {"enabled": True},
            "get_hyper3d_status": {"enabled": False},
            "create_hunyuan_job": {"Response": {"JobId": "a"}},
            "poll_hunyuan_job_status": {"Response": {"Status": "DONE", "ResultFile3Ds": [
                {"Type": "GLB", "Url": "https://x/model.glb"}]}},
            "import_generated_asset_hunyuan": {"succeed": True},
        }.get(command, {})


def _connect(monkeypatch, protocol, error=None):
    blender = FakeBlender(error)
    monkeypatch.setattr(server, "get_blender_connection", lambda: blender)
    handshake = None if protocol is None else AddonHandshake(True, protocol, [2, 1], [], "4.2", "native")
    monkeypatch.setattr(server, "_addon_handshake", handshake)
    return blender


@pytest.mark.parametrize("protocol, sends_quality", [(None, False), (9, False), (10, False), (11, True)])
def test_hunyuan_quality_reaches_only_addons_that_accept_it(monkeypatch, protocol, sends_quality):
    blender = _connect(monkeypatch, protocol)
    out = asyncio.run(server.generate_3d(None, prompt="stool", provider="hunyuan3d", quality="high"))
    assert out.startswith("Generated and imported 'Stool' with hunyuan3d"), out
    create = next(params for command, params in blender.sent if command == "create_hunyuan_job")
    assert ("quality" in create) is sends_quality


@pytest.mark.parametrize("kwargs", [
    {"prompt": "stool", "provider": "tripo"},
    {"job": "tripo:rid:r", "name": "n"},
])
def test_tripo_explains_itself_when_the_addon_has_no_tripo(monkeypatch, kwargs):
    _connect(monkeypatch, 9, error="Unknown command type: x")
    out = asyncio.run(server.generate_3d(None, **kwargs))
    assert out == f"Error: {server.TRIPO_UNAVAILABLE}"
