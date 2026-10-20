"""get_viewport_screenshot returns the image over the socket (#372).

The server used to pick a temp path, have Blender save the PNG there and read
it back. That only works when both share a filesystem: with the server in
Docker and Blender on Windows, Blender writes C:\\tmp\\blender_screenshot_1.png,
answers success, and the server finds nothing at /tmp/blender_screenshot_1.png.
Protocol 14 addons send the bytes back instead; older ones keep the path.
"""
import base64
import importlib.util
import os
import sys
import types
from contextlib import contextmanager

import pytest

from blender_mcp import server
from conftest import ROOT_ADDON as ADDON

PNG = b"\x89PNG\r\n\x1a\nviewport"


# --- server side -------------------------------------------------------------

def _connect(monkeypatch, protocol, reply):
    sent = []

    def send_command(command, params=None):
        sent.append((command, params))
        return reply(params)

    monkeypatch.setattr(server, "get_blender_connection", lambda: types.SimpleNamespace(send_command=send_command))
    monkeypatch.setattr(server, "_addon_protocol", lambda: protocol)
    return sent


def test_image_comes_back_over_the_socket(monkeypatch):
    encoded = base64.b64encode(PNG).decode("ascii")
    sent = _connect(monkeypatch, 14, lambda _params: {"success": True, "width": 4, "image_data": encoded})

    png, info = server._capture_viewport(800)

    assert png == PNG
    assert info == {"success": True, "width": 4}
    assert sent == [("get_viewport_screenshot", {"max_size": 800, "format": "png", "return_data": True})]


def test_missing_image_data_is_an_error(monkeypatch):
    _connect(monkeypatch, 14, lambda _params: {"success": True})

    with pytest.raises(Exception, match="Screenshot data was not returned"):
        server._capture_viewport(800)


def test_addon_error_is_raised(monkeypatch):
    _connect(monkeypatch, 14, lambda _params: {"error": "No 3D viewport found"})

    with pytest.raises(Exception, match="No 3D viewport found"):
        server._capture_viewport(800)


@pytest.mark.parametrize("protocol", [None, 13])
def test_older_addons_still_write_to_a_path(monkeypatch, protocol):
    def reply(params):
        with open(params["filepath"], "wb") as fh:
            fh.write(PNG)
        return {"success": True}

    sent = _connect(monkeypatch, protocol, reply)

    png, _info = server._capture_viewport(800)

    assert png == PNG
    params = sent[0][1]
    assert "return_data" not in params
    assert not os.path.exists(params["filepath"])


# --- addon side --------------------------------------------------------------

def _load_addon(monkeypatch, grab):
    """addon.py on a stub bpy with one 3D viewport and no gpu module.

    Without gpu the offscreen render fails and the window grab runs, which
    writes through `grab(filepath)`.
    """
    region = types.SimpleNamespace(type="WINDOW")
    area = types.SimpleNamespace(type="VIEW_3D", spaces=types.SimpleNamespace(active=object()), regions=[region])
    scene = types.SimpleNamespace(name="Scene")

    @contextmanager
    def temp_override(**_kwargs):
        yield

    loaded = []

    def load(filepath):
        img = types.SimpleNamespace(size=(4, 2), filepath=filepath)
        loaded.append(img)
        return img

    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        screen=types.SimpleNamespace(areas=[area]),
        temp_override=temp_override,
    )
    bpy.data = types.SimpleNamespace(
        filepath="",
        scenes=[scene],
        images=types.SimpleNamespace(load=load, remove=lambda img: None),
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace(screen=types.SimpleNamespace(screenshot_area=lambda filepath: grab(filepath)))

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 2, 0)
    app.version_string = "4.2.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))
    monkeypatch.setitem(sys.modules, "gpu", None)  # import gpu raises ImportError

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def _writing_grab(paths):
    def grab(filepath):
        paths.append(filepath)
        with open(filepath, "wb") as fh:
            fh.write(PNG)
    return grab


def test_addon_returns_the_image_and_removes_its_temp_file(monkeypatch):
    paths = []
    addon = _load_addon(monkeypatch, _writing_grab(paths))

    result = addon.BlenderMCPServer().get_viewport_screenshot(max_size=800, return_data=True)

    assert result["success"] is True
    assert base64.b64decode(result["image_data"]) == PNG
    assert "filepath" not in result
    assert len(paths) == 1 and not os.path.exists(paths[0])


def test_addon_still_writes_to_a_given_path(monkeypatch, tmp_path):
    addon = _load_addon(monkeypatch, _writing_grab([]))
    out = str(tmp_path / "shot.png")

    result = addon.BlenderMCPServer().get_viewport_screenshot(max_size=800, filepath=out)

    assert result["filepath"] == out
    assert "image_data" not in result
    with open(out, "rb") as fh:
        assert fh.read() == PNG


def test_addon_needs_a_path_or_return_data(monkeypatch):
    addon = _load_addon(monkeypatch, _writing_grab([]))

    assert addon.BlenderMCPServer().get_viewport_screenshot() == {"error": "No filepath provided"}


def test_addon_removes_its_temp_file_when_capture_fails(monkeypatch):
    paths = []

    def failing_grab(filepath):
        paths.append(filepath)
        raise RuntimeError("no window")

    addon = _load_addon(monkeypatch, failing_grab)

    result = addon.BlenderMCPServer().get_viewport_screenshot(return_data=True)

    assert result == {"error": "no window"}
    assert len(paths) == 1 and not os.path.exists(paths[0])
