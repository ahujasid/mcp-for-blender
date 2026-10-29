"""The model-facing surface: a handful of tools and observation scripts that run
through execute_code."""

import ast
import asyncio

from blender_mcp import blender_scripts, guides, server
from blender_mcp.openai_apps import is_app_only

MODEL_TOOLS = {
    "get_addon_status", "disable_telemetry", "get_scene_info", "execute_blender_code",
    "record_trajectory_feedback", "look", "generate_3d", "generate_image", "search_assets", "import_asset",
}


def test_model_sees_only_the_consolidated_tools():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools if not is_app_only(t)} == MODEL_TOOLS


def test_every_guide_has_a_title_summary_and_body():
    found = guides.all_guides()
    assert {"bpy", "scene", "level-design", "animation", "rigging", "retopology", "materials"} <= set(found)
    for guide in found.values():
        assert guide.title and guide.summary and guide.body.startswith("# ")


def test_guides_are_off_for_now():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    assert "get_guide" not in tools and not any(u.startswith("guide://") for u in uris)


def test_scripts_compile_and_never_raise_systemexit():
    # The addon catches Exception around execute_code but not SystemExit.
    for script in (blender_scripts.SCENE_SUMMARY, blender_scripts.LOOK, blender_scripts.BOUNDS):
        code = blender_scripts.build(script, {"mode": "angles", "target": None, "flag": True, "none": None})
        ast.parse(code)
        assert "SystemExit" not in code and "sys.exit" not in code


def test_script_arguments_round_trip_json_only_values():
    code = blender_scripts.build("return ARGS", {"a": True, "b": None, "c": ["x'y\"z"]})
    namespace = {}
    import contextlib, io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code, namespace)
    assert blender_scripts.parse_result("noise\n" + out.getvalue()) == {"a": True, "b": None, "c": ["x'y\"z"]}


def test_retired_look_modes_point_to_their_replacements(monkeypatch):
    monkeypatch.setattr(server, "_run_script", lambda *a: (_ for _ in ()).throw(AssertionError("ran")))
    text = _look_text(asyncio.run(server.look(None, mode="topology")))
    assert 'shading="wireframe"' in text and '"topology"' in text
    text = _look_text(asyncio.run(server.look(None, mode="rig")))
    assert 'shading="xray"' in text and '"weights"' in text
    result = asyncio.run(server.look(None, mode="camera", image="Render Result"))
    assert result.isError and "leave mode unset" in _look_text(result)


def test_scene_summary_formatting():
    text = server._format_scene_summary({
        "header": {"scene": "Scene", "object_counts": {"mesh": 60}, "selected": ["A", "B", "C", "D", "E"],
                   "selected_count": 7, "active": None, "mode": "OBJECT"},
        "lines": ["Cube | mesh | at (0, 0, 1)"], "total": 60, "shown": 1,
    }, ["location"])
    assert "Cube | mesh" in text
    assert "59 more" in text
    assert "+2 more" in text
    assert "(name | type | location)" in text
    assert "engine" not in text


def test_scene_summary_settings_line_is_opt_in():
    text = server._format_scene_summary({
        "header": {"scene": "Scene", "object_counts": {}, "selected": [], "active": None, "mode": "OBJECT",
                   "settings": {"file": "(unsaved)", "engine": "CYCLES", "frames": [1, 250, 1], "fps": 24,
                                "resolution": [1920, 1080], "camera": "Camera", "world_hdri": None,
                                "unit_scale": 1.0}},
        "lines": [], "total": 0, "shown": 0,
    }, ["settings"])
    assert "engine CYCLES" in text and "1920x1080" in text


def test_scene_info_sends_only_the_requested_fields(monkeypatch):
    sent = []

    def fake_run(script, args):
        sent.append(args)
        return {"header": {"scene": "S", "object_counts": {}, "selected": [], "active": None, "mode": "OBJECT"},
                "lines": [], "total": 0, "shown": 0}

    monkeypatch.setattr(server, "_run_script", fake_run)
    asyncio.run(server.get_scene_info(None))
    assert sent[-1]["fields"] == list(blender_scripts.SCENE_DEFAULT_FIELDS)
    assert sent[-1]["limit"] == 20
    asyncio.run(server.get_scene_info(None, fields=["materials", "materials"]))
    assert sent[-1]["fields"] == ["materials"]
    reply = asyncio.run(server.get_scene_info(None, fields=["colour"]))
    assert reply.startswith("Error") and "materials" in reply


class _Blender:
    def __init__(self, replies):
        self.replies = replies
        self.sent = []

    def send_command(self, command, params=None, read_only=False):
        self.sent.append((command, params))
        reply = self.replies.get(command)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_texture_import_applies_the_material(monkeypatch):
    blender = _Blender({
        "download_polyhaven_asset": {"success": True, "message": "Downloaded", "material": "Rock",
                                     "maps": ["diffuse"]},
        "set_texture": {"success": True, "material": "Rock", "maps": ["diffuse"]},
    })
    monkeypatch.setattr(server, "get_blender_connection", lambda: blender)
    reply = asyncio.run(server.import_asset(None, source="polyhaven", id="rock", asset_type="textures",
                                            apply_to=["Ground"]))
    assert ("set_texture", {"object_name": "Ground", "texture_id": "rock"}) in blender.sent
    assert "Applied" in reply and server.POLYHAVEN_UNUSED_NOTE not in reply


def test_sketchfab_import_needs_a_size():
    reply = asyncio.run(server.import_asset(None, source="sketchfab", id="abc"))
    assert reply.startswith("Error") and "target_size" in reply


def test_a_missing_library_command_means_switched_off_or_outdated(monkeypatch):
    from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake

    blender = _Blender({"search_sketchfab_models": Exception("Unknown command type: search_sketchfab_models")})
    monkeypatch.setattr(server, "get_blender_connection", lambda: blender)

    current = AddonHandshake(True, EXPECTED_ADDON_PROTOCOL_VERSION, [2, 1], [], "4.2", "native")
    monkeypatch.setattr(server, "_addon_handshake", current)
    reply = asyncio.run(server.search_assets(None, source="sketchfab", query="car"))
    assert "switched off" in reply and "install-addon" not in reply

    behind = AddonHandshake(False, 9, [2, 0], [], "4.2", "native")
    monkeypatch.setattr(server, "_addon_handshake", behind)
    reply = asyncio.run(server.search_assets(None, source="sketchfab", query="car"))
    assert "install-addon" in reply


def _look_text(result):
    return " ".join(getattr(c, "text", "") for c in result.content)


def test_look_rejects_bad_views_before_touching_blender(monkeypatch):
    monkeypatch.setattr(server, "_run_script", lambda *a: (_ for _ in ()).throw(AssertionError("ran")))
    result = asyncio.run(server.look(None, mode="angles", views=["sideways"]))
    assert result.isError and "three_quarter" in _look_text(result)
    result = asyncio.run(server.look(None, mode="angles", views=[[0, 0, 0]]))
    assert result.isError and "not all zero" in _look_text(result)


def test_look_passes_directions_and_distance_through(monkeypatch):
    sent = []

    def fake_run(script, args):
        sent.append(args)
        return {"error": "stop here"}

    monkeypatch.setattr(server, "_run_script", fake_run)
    asyncio.run(server.look(None, mode="angles", views=[[0, -1, 0.2], "top"], distance=3.0))
    assert sent[-1]["views"] == [[0, -1, 0.2], "top"] and sent[-1]["distance"] == 3.0


def test_image_mode_never_falls_back_to_the_viewport(monkeypatch):
    def broken(*a):
        raise RuntimeError("addon too old")

    monkeypatch.setattr(server, "_run_script", broken)
    monkeypatch.setattr(server, "_viewport_screenshot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell back")))
    result = asyncio.run(server.look(None, image="Render Result"))
    assert result.isError and "Couldn't show the image" in _look_text(result)


def test_look_caption_for_images():
    assert server._look_caption({"mode": "image", "image": "Render Result", "original_size": [1920, 1080],
                                 "width": 768, "height": 432}) == \
        "Image 'Render Result', 1920x1080, shown at 768x432."
