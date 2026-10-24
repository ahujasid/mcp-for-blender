"""Pointing at part of a mesh: the face a click or the 3D cursor lands on, and
the connected piece around it.

Imported models often merge many parts into one object, so naming the object
says little about what the user meant. The PICK script runs in Blender; here
its result is faked and what the model and the Viewport app get is checked.
"""

import asyncio
import base64
import types

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from blender_mcp import blender_scripts, openai_apps, server

VIEW = {"view_matrix": [[1, 0, 0, 0]] * 4, "window_matrix": [[1, 0, 0, 0]] * 4, "width": 800, "height": 600}

TRIM = {
    "object": "Object_30.002", "face": 15709, "location": [10.03, -7.028, 1.9104], "distance": 0.0004,
    "material": "Material.016", "object_faces": 28484, "face_index_is_in_object_data": True,
    "piece": {"faces": 644, "min": [9.459, -7.116, 1.8562], "max": [12.061, -6.8558, 1.9236],
              "size": [2.602, 0.2602, 0.0674]},
}

SUMMARY = {
    "header": {"scene": "Scene", "object_counts": {"mesh": 693}, "selected": [], "active": None, "mode": "OBJECT"},
    "lines": ["Object_30.002 | mesh | at (10.76, -4.15, 1.42)"], "total": 1, "shown": 1,
}


def _scene_info(monkeypatch, pick, fields=("location", "cursor")):
    calls = []

    def run_script(script, args):
        calls.append((script, args))
        if script is blender_scripts.PICK:
            if isinstance(pick, Exception):
                raise pick
            return pick
        return SUMMARY

    monkeypatch.setattr(server, "_run_script", run_script)
    text = asyncio.run(server.get_scene_info(None, fields=list(fields)))
    return text, calls


# ---------------------------------------------------------------- the 3D cursor

def test_cursor_says_which_face_and_piece_it_is_on(monkeypatch):
    text, calls = _scene_info(monkeypatch, {"hit": TRIM})
    assert calls[1] == (blender_scripts.PICK, {"mode": "cursor"})
    line = next(l for l in text.splitlines() if l.startswith("3D cursor"))
    assert "'Object_30.002', face 15709 (material 'Material.016')" in line
    assert "connected piece of 644 of its 28484 faces, 2.602 x 0.2602 x 0.0674 m" in line
    assert "from (9.459, -7.116, 1.8562) to (12.061, -6.8558, 1.9236)" in line
    # A header line, not a column on every object line.
    assert "(name | type | location)" in text


def test_cursor_on_nothing_says_how_the_user_points(monkeypatch):
    text, _ = _scene_info(monkeypatch, {"hit": None, "cursor": [0, 0, 0]})
    assert "3D cursor at (0, 0, 0) isn't on any visible mesh" in text
    assert "Shift+right-click" in text


def test_cursor_far_from_the_surface_is_flagged(monkeypatch):
    text, _ = _scene_info(monkeypatch, {"hit": {**TRIM, "distance": 0.8}})
    assert "probably not placed on it" in text


def test_a_whole_object_piece_says_so(monkeypatch):
    whole = {**TRIM, "object_faces": 644}
    text, _ = _scene_info(monkeypatch, {"hit": whole})
    assert "one connected piece: the whole object (644 faces" in text


def test_faces_after_modifiers_are_labelled(monkeypatch):
    text, _ = _scene_info(monkeypatch, {"hit": {**TRIM, "face_index_is_in_object_data": False}})
    assert "face 15709 of the mesh after modifiers" in text


def test_a_failed_cursor_check_keeps_the_rest_of_the_summary(monkeypatch):
    text, _ = _scene_info(monkeypatch, RuntimeError("boom"))
    assert "couldn't tell what it is on (boom)" in text
    assert "Object_30.002 | mesh" in text


def test_the_cursor_is_only_checked_when_asked_for(monkeypatch):
    _, calls = _scene_info(monkeypatch, {"hit": TRIM}, fields=("location",))
    assert [script for script, _ in calls] == [blender_scripts.SCENE_SUMMARY]


# ---------------------------------------------------------------- Viewport app clicks

def _fresh_store(monkeypatch):
    store = openai_apps.ViewportStore()
    monkeypatch.setattr(server, "viewport_store", store)
    monkeypatch.setattr(server, "_capture_viewport", lambda _size: (b"\x89PNG", {"view": VIEW}))
    server.viewport_capture()


def _click(monkeypatch, obj, pick):
    sent = []

    def send_command(name, params):
        sent.append(name)
        return {"object": obj}

    def run_script(script, args):
        sent.append(("script", args))
        if isinstance(pick, Exception):
            raise pick
        return pick

    monkeypatch.setattr(server, "get_blender_connection", lambda: types.SimpleNamespace(send_command=send_command))
    monkeypatch.setattr(server, "_run_script", run_script)
    return server.viewport_pick(seq=1, x=0.25, y=0.5).structuredContent["object"], sent


def test_clicking_a_mesh_names_the_face_and_piece(monkeypatch):
    _fresh_store(monkeypatch)
    obj, sent = _click(monkeypatch, {"name": "Object_30.002", "type": "MESH", "detail": "Mesh object"},
                       {"hit": TRIM})
    assert sent[0] == "pick_viewport_object"
    assert sent[1] == ("script", {"mode": "ray", "view_matrix": VIEW["view_matrix"],
                                  "window_matrix": VIEW["window_matrix"], "x": 0.25, "y": 0.5})
    assert obj["surface"]["piece"]["faces"] == 644
    # The app and the link both show `detail`, so the face and piece reach the model.
    assert obj["detail"].startswith("Mesh object; face 15709")
    assert obj["link"]["description"] == obj["detail"]


@pytest.mark.parametrize("obj, pick", [
    ({"name": "Lamp", "type": "LIGHT", "detail": "Light object"}, {"hit": TRIM}),
    ({"name": "Object_30.002", "type": "MESH", "detail": "Mesh object"}, RuntimeError("old addon")),
    ({"name": "Windshield", "type": "MESH", "detail": "Mesh object"}, {"hit": TRIM}),  # ray hit something else
    ({"name": "Object_30.002", "type": "MESH", "detail": "Mesh object"}, {"hit": None}),
])
def test_clicks_without_a_face_still_pick_the_object(monkeypatch, obj, pick):
    _fresh_store(monkeypatch)
    picked, sent = _click(monkeypatch, obj, pick)
    assert picked["name"] == obj["name"] and picked["detail"] == obj["detail"] and "surface" not in picked
    if obj["type"] != "MESH":
        assert sent == ["pick_viewport_object"]


# ---------------------------------------------------------------- look(save_to=)

PNG = b"\x89PNG\r\n\x1a\nfake"


def _fake_look(monkeypatch, result):
    monkeypatch.setattr(server, "_look_via_script", lambda args: result)


def test_look_saves_the_image_for_the_user(monkeypatch, tmp_path):
    _fake_look(monkeypatch, CallToolResult(content=[
        ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png"),
        TextContent(type="text", text="Tiles left to right"),
    ]))
    target = tmp_path / "shots" / "dash.png"
    result = asyncio.run(server.look(None, mode="angles", save_to=str(target)))
    assert target.read_bytes() == PNG
    assert f"Saved this image to {target}." in result.content[-1].text
    assert isinstance(result.content[0], ImageContent)  # the model still sees it


def test_look_refuses_a_non_png_path_before_touching_blender(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_look_via_script", lambda args: (_ for _ in ()).throw(AssertionError("ran")))
    result = asyncio.run(server.look(None, mode="angles", save_to=str(tmp_path / "notes.txt")))
    assert result.isError and ".png" in result.content[0].text
    assert not (tmp_path / "notes.txt").exists()


def test_look_without_save_to_writes_nothing(monkeypatch, tmp_path):
    image = ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")
    _fake_look(monkeypatch, CallToolResult(content=[image]))
    result = asyncio.run(server.look(None, mode="angles"))
    assert result.content == [image]


def test_a_failed_save_is_reported_not_raised(monkeypatch, tmp_path):
    _fake_look(monkeypatch, CallToolResult(content=[
        ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")]))
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    result = asyncio.run(server.look(None, mode="angles", save_to=str(blocker / "x.png")))
    assert not result.isError and "Couldn't save the image" in result.content[-1].text
