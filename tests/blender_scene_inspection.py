import importlib.util
from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inspection_test_addon", root / "addon.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
server = addon.BlenderMCPServer()

for i in range(25):
    obj = bpy.data.objects.new(f"SearchFixture_{i:02}", None)
    bpy.context.scene.collection.objects.link(obj)
page = server.inspect_scene(query="searchfixture_", limit=10)
assert page["total_matches"] == 25 and page["next_offset"] == 10
assert len(server.inspect_scene(query="SearchFixture_", offset=20, limit=10)["objects"]) == 5
assert server.inspect_scene(query="SearchFixture_", object_type="MESH")["total_matches"] == 0
assert server.inspect_scene(query="absent")["next_offset"] is None

cube = bpy.data.objects["Cube"]
vertices = [tuple(v.co) for v in cube.data.vertices]
selection = [o.name for o in bpy.context.selected_objects]
frame = bpy.context.scene.frame_current
report = server.validate_mesh(cube.name, triangle_budget=11)
assert report["counts"]["triangles"] == 12 and report["within_triangle_budget"] is False
assert server.validate_mesh(cube.name, triangle_budget=12)["within_triangle_budget"] is True
assert server.validate_mesh(cube.name)["within_triangle_budget"] is None
assert report["counts"]["boundary_edges"] == 0
modifier = cube.modifiers.new("Subdivision fixture", "SUBSURF")
modifier.levels = 1
assert server.validate_mesh(cube.name)["counts"]["triangles"] > 12
assert [tuple(v.co) for v in cube.data.vertices] == vertices
assert len(cube.data.polygons) == 6
assert [o.name for o in bpy.context.selected_objects] == selection
assert bpy.context.scene.frame_current == frame

mesh = bpy.data.meshes.new("TopologyFixture")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, -1, 0),
                  (0, 0, 1), (2, 2, 2), (3, 3, 3),
                  (5, 0, 0), (6, 0, 0), (7, 0, 0)],
                 [(5, 6)], [(0, 1, 2), (1, 0, 3), (0, 1, 4), (7, 8, 9)])
obj = bpy.data.objects.new("TopologyFixture", mesh)
bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.update()
report = server.validate_mesh(obj.name)
assert report["counts"]["overused_edges"] == 1
assert report["counts"]["loose_edges"] == 1
assert report["counts"]["zero_area_faces"] == 1
assert report["counts"]["boundary_edges"] == 9

collection = bpy.data.collections.new("ExcludedFixture")
bpy.context.scene.collection.children.link(collection)
excluded = bpy.data.objects.new("ExcludedMesh", mesh.copy())
collection.objects.link(excluded)
bpy.context.view_layer.layer_collection.children[collection.name].exclude = True
bpy.context.view_layer.update()
row = server.inspect_scene(query=excluded.name)["objects"][0]
assert not row["in_view_layer"] and not row["selected"] and not row["visible"]
try:
    server.validate_mesh(excluded.name)
    raise AssertionError("Excluded mesh was accepted")
except ValueError as exc:
    assert "active view layer" in str(exc)

assert {"inspect_scene", "validate_mesh"} <= set(server.get_addon_info()["capabilities"])
assert (root / "addon.py").read_bytes() == (root / "src/blender_mcp/bundled/addon.py").read_bytes()
print("SCENE_INSPECTION_BLENDER_PASSED", bpy.app.version_string)
