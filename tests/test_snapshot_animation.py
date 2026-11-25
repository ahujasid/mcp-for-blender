"""_snapshot_animation: support both legacy Action.fcurves and Blender 4.4+/5.0+ slotted actions."""
import importlib.util
import sys
import types
from typing import Any

from conftest import ROOT_ADDON as ADDON


def _load_addon_module(monkeypatch, anim_utils_mod=None) -> Any:
    scene = types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=False,
        objects=(),
    )
    bpy: Any = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(scene=scene)
    bpy.data = types.SimpleNamespace(filepath="")
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace()

    props: Any = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers: Any = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app: Any = types.ModuleType("bpy.app")
    app.version = (5, 3, 0)
    app.version_string = "5.3.0"
    app.background = True
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

    requests: Any = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    if anim_utils_mod is not None:
        bpy_extras: Any = types.ModuleType("bpy_extras")
        bpy_extras.anim_utils = anim_utils_mod
        monkeypatch.setitem(sys.modules, "bpy_extras", bpy_extras)
        monkeypatch.setitem(sys.modules, "bpy_extras.anim_utils", anim_utils_mod)
    else:
        monkeypatch.delitem(sys.modules, "bpy_extras", raising=False)
        monkeypatch.delitem(sys.modules, "bpy_extras.anim_utils", raising=False)

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_anim_test", ADDON)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kp(frame: float):
    return types.SimpleNamespace(co=types.SimpleNamespace(x=frame))


def _fcurve(data_path: str, array_index: int, frames: list[float]):
    return types.SimpleNamespace(
        data_path=data_path,
        array_index=array_index,
        keyframe_points=[_kp(f) for f in frames],
    )


def test_legacy_action_fcurves_snapshot(monkeypatch):
    module = _load_addon_module(monkeypatch)
    action = types.SimpleNamespace(
        name="CubeAction",
        fcurves=[
            _fcurve("location", 0, [1.0, 24.0]),
            _fcurve("location", 2, [5.0, 48.0]),
        ],
    )
    anim_data = types.SimpleNamespace(action=action, drivers=None, nla_tracks=[])
    obj = types.SimpleNamespace(animation_data=anim_data)

    snap = module.BlenderMCPServer._snapshot_animation(obj)
    assert snap == {
        "animation": {
            "action": "CubeAction",
            "channels": [
                {"data_path": "location", "array_index": 0, "keyframes": 2},
                {"data_path": "location", "array_index": 2, "keyframes": 2},
            ],
            "keyframe_count": 4,
            "frame_range": [1.0, 48.0],
        }
    }


def test_slotted_action_without_fcurves_uses_anim_utils(monkeypatch):
    """Blender 5.0+ removed Action.fcurves; channelbag lookup via anim_utils must succeed."""
    slot = object()
    channelbag = types.SimpleNamespace(
        slot=slot,
        fcurves=[
            _fcurve("location", 0, [1.0, 12.0, 24.0]),
            _fcurve("rotation_euler", 2, [1.0, 36.0]),
        ],
    )
    anim_utils: Any = types.ModuleType("bpy_extras.anim_utils")
    anim_utils.action_get_channelbag_for_slot = (
        lambda act, s: channelbag if s is slot else None
    )
    module = _load_addon_module(monkeypatch, anim_utils_mod=anim_utils)

    # Deliberately omit `fcurves` attribute to mirror Blender 5.0+ Action RNA.
    action = types.SimpleNamespace(name="SlottedWalk")
    anim_data = types.SimpleNamespace(
        action=action,
        action_slot=slot,
        drivers=[object()],
        nla_tracks=[types.SimpleNamespace(name="WalkTrack")],
    )
    obj = types.SimpleNamespace(animation_data=anim_data)

    snap = module.BlenderMCPServer._snapshot_animation(obj)
    assert snap == {
        "animation": {
            "action": "SlottedWalk",
            "channels": [
                {"data_path": "location", "array_index": 0, "keyframes": 3},
                {"data_path": "rotation_euler", "array_index": 2, "keyframes": 2},
            ],
            "keyframe_count": 5,
            "frame_range": [1.0, 36.0],
            "driver_count": 1,
            "nla_tracks": ["WalkTrack"],
        }
    }


def test_slotted_action_falls_back_to_layers_and_preserves_unassigned_slot(monkeypatch):
    """Without bpy_extras (or when action_slot is None in 5.0+), layers fallback and metadata survive."""
    module = _load_addon_module(monkeypatch, anim_utils_mod=None)
    slot = object()
    cb = types.SimpleNamespace(slot=slot, fcurves=[_fcurve("scale", 1, [10.0, 20.0])])
    strip = types.SimpleNamespace(channelbags=[cb])
    layer = types.SimpleNamespace(strips=[strip])
    action = types.SimpleNamespace(name="LayerFallbackAction", layers=[layer])

    anim_data = types.SimpleNamespace(action=action, action_slot=slot, drivers=None, nla_tracks=[])
    obj = types.SimpleNamespace(animation_data=anim_data)
    snap = module.BlenderMCPServer._snapshot_animation(obj)
    assert snap["animation"]["action"] == "LayerFallbackAction"
    assert snap["animation"]["keyframe_count"] == 2
    assert snap["animation"]["frame_range"] == [10.0, 20.0]

    # Unassigned slot on Blender 5.0+ (no Action.fcurves) still preserves action name & NLA tracks.
    empty_action = types.SimpleNamespace(name="UnboundAction", layers=[])
    anim_unbound = types.SimpleNamespace(
        action=empty_action,
        action_slot=None,
        drivers=[object(), object()],
        nla_tracks=[types.SimpleNamespace(name="BaseTrack")],
    )
    snap_unbound = module.BlenderMCPServer._snapshot_animation(
        types.SimpleNamespace(animation_data=anim_unbound)
    )
    assert snap_unbound == {
        "animation": {
            "action": "UnboundAction",
            "keyframe_count": 0,
            "driver_count": 2,
            "nla_tracks": ["BaseTrack"],
        }
    }


def test_bundled_addon_matches_root_addon():
    bundled = ADDON.parent / "src" / "blender_mcp" / "bundled" / "addon.py"
    assert ADDON.read_bytes() == bundled.read_bytes()
