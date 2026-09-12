import ast
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import ROOT_ADDON


def addon_method(name, bpy):
    tree = ast.parse(ROOT_ADDON.read_text(encoding="utf-8"))
    server = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BlenderMCPServer")
    node = next(n for n in server.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {"bpy": bpy}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT_ADDON), "exec"), namespace)
    return namespace[name]


def scene_object(name, kind="MESH"):
    return SimpleNamespace(name=name, type=kind, parent=None, mode="OBJECT",
                           select_get=Mock(return_value=False), visible_get=Mock(return_value=True),
                           dimensions=(1, 2, 3), matrix_world=SimpleNamespace(translation=(4, 5, 6)),
                           users_collection=[SimpleNamespace(name="Collection")], modifiers=[])


def inspection_context(objects, excluded=()):
    scene = SimpleNamespace(name="Scene", objects=objects,
                            unit_settings=SimpleNamespace(system="METRIC", scale_length=1))
    layer = SimpleNamespace(objects={o.name: o for o in objects if o.name not in excluded})
    return SimpleNamespace(context=SimpleNamespace(scene=scene, view_layer=layer, mode="OBJECT"),
                           app=SimpleNamespace(version_string="test"))


def test_search_paginates_filtered_names_in_order():
    objects = [scene_object(f"Part_{i:02}") for i in reversed(range(25))]
    objects.append(scene_object("Part_Light", "LIGHT"))
    bpy = inspection_context(objects)
    inspect = addon_method("inspect_scene", bpy)
    names = []
    offset = 0
    while offset is not None:
        page = inspect(None, offset=offset, limit=10, query="pArT_", object_type="mesh")
        assert page["total_matches"] == 25
        assert len(page["objects"]) <= 10
        names.extend(row["name"] for row in page["objects"])
        offset = page["next_offset"]
    assert names == [f"Part_{i:02}" for i in range(25)]
    assert inspect(None, offset=100)["next_offset"] is None
    assert inspect(None, query="absent")["objects"] == []


def test_excluded_objects_do_not_query_view_layer_state():
    obj = scene_object("Excluded")
    obj.select_get.side_effect = RuntimeError("not in view layer")
    obj.visible_get.side_effect = RuntimeError("not in view layer")
    row = addon_method("inspect_scene", inspection_context([obj], [obj.name]))(None)["objects"][0]
    assert row["in_view_layer"] is False
    assert row["selected"] is False and row["visible"] is False
    assert row["dimensions"] == [1, 2, 3] and row["world_position"] == [4, 5, 6]
    obj.select_get.assert_not_called()
    obj.visible_get.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"offset": -1}, {"offset": True}, {"offset": 0.5},
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"limit": 1.5},
    {"query": None}, {"object_type": None},
])
def test_search_rejects_invalid_parameters_before_accessing_blender(arguments):
    with pytest.raises(ValueError):
        addon_method("inspect_scene", None)(None, **arguments)


@pytest.mark.parametrize("arguments", [
    {"triangle_budget": -1}, {"triangle_budget": True}, {"triangle_budget": 2.5},
    {"area_epsilon": -1}, {"area_epsilon": float("nan")},
    {"area_epsilon": float("inf")}, {"area_epsilon": True}, {"area_epsilon": "invalid"},
])
def test_mesh_rejects_invalid_thresholds_before_accessing_blender(arguments):
    with pytest.raises(ValueError):
        addon_method("validate_mesh", None)(None, "Cube", **arguments)


@pytest.mark.parametrize("kind,mode,in_scene,in_layer", [
    ("LIGHT", "OBJECT", True, True), ("MESH", "EDIT", True, True),
    ("MESH", "OBJECT", False, True), ("MESH", "OBJECT", True, False),
])
def test_mesh_requires_an_object_mode_mesh_in_the_current_scene_and_layer(kind, mode, in_scene, in_layer):
    obj = scene_object("Target", kind)
    obj.mode = mode
    bpy = inspection_context([obj])
    bpy.context.scene.objects = {obj.name: obj} if in_scene else {}
    bpy.context.view_layer.objects = {obj.name: obj} if in_layer else {}
    with pytest.raises(ValueError):
        addon_method("validate_mesh", bpy)(None, obj.name)


def test_evaluated_mesh_is_released_when_triangulation_fails():
    obj = scene_object("Target")
    mesh = SimpleNamespace(calc_loop_triangles=Mock(side_effect=RuntimeError("evaluation failed")))
    evaluated = SimpleNamespace(to_mesh=Mock(return_value=mesh), to_mesh_clear=Mock())
    obj.evaluated_get = Mock(return_value=evaluated)
    bpy = inspection_context([obj])
    bpy.context.scene.objects = {obj.name: obj}
    bpy.context.evaluated_depsgraph_get = Mock(return_value=object())
    with pytest.raises(RuntimeError, match="evaluation failed"):
        addon_method("validate_mesh", bpy)(None, obj.name)
    evaluated.to_mesh_clear.assert_called_once_with()


def test_tools_are_discoverable_and_forward_their_arguments(monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_DISABLE_TELEMETRY", "true")
    from blender_mcp import server
    connection = Mock()
    connection.send_command.return_value = {"result": "fixture"}
    monkeypatch.setattr(server, "get_blender_connection", lambda: connection)
    result = asyncio.run(server.inspect_scene(None, offset=10, limit=20, query="Part", object_type="MESH"))
    assert result == {"result": "fixture"}
    connection.send_command.assert_called_with("inspect_scene", {
        "offset": 10, "limit": 20, "query": "Part", "object_type": "MESH"})
    asyncio.run(server.validate_mesh(None, "Part", triangle_budget=100, area_epsilon=1e-10))
    connection.send_command.assert_called_with("validate_mesh", {
        "name": "Part", "triangle_budget": 100, "area_epsilon": 1e-10})
    catalog = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    for name in ("inspect_scene", "validate_mesh"):
        assert catalog[name].annotations.readOnlyHint
        assert not catalog[name].annotations.openWorldHint
        assert len(catalog[name].description) > 80
    assert "next_offset" in catalog["inspect_scene"].description
    assert "object-local" in catalog["validate_mesh"].description
