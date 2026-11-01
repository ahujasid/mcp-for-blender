"""Shader change detection without a Blender installation."""

import ast
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
import zlib

import pytest

from conftest import ROOT_ADDON


@pytest.fixture(params=[ROOT_ADDON, ROOT_ADDON.parent / "src/blender_mcp/bundled/addon.py"])
def fingerprint(request):
    tree = ast.parse(Path(request.param).read_text(encoding="utf-8"))
    server = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "BlenderMCPServer")
    server.body = [node for node in server.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_shader_fingerprint"]
    namespace = {"zlib": zlib, "suppress": suppress}
    exec(compile(ast.Module(body=[server], type_ignores=[]), str(request.param), "exec"), namespace)
    return namespace["BlenderMCPServer"]._shader_fingerprint


def _material():
    color = SimpleNamespace(name="Color", bl_idname="ShaderNodeRGB", inputs=[])
    shader = SimpleNamespace(name="Shader", bl_idname="ShaderNodeBsdfPrincipled", inputs=[])
    output = SimpleNamespace(name="Output", bl_idname="ShaderNodeOutputMaterial", inputs=[])
    link = SimpleNamespace(from_node=color, from_socket=SimpleNamespace(identifier="Color"),
                           to_node=shader, to_socket=SimpleNamespace(identifier="Base Color"))
    tree = SimpleNamespace(nodes=[color, shader, output], links=[link])
    return SimpleNamespace(use_nodes=True, node_tree=tree), link


def test_rewiring_changes_fingerprint_with_same_link_count(fingerprint):
    material, link = _material()
    before = fingerprint(material)
    assert before is not None
    link.to_socket = SimpleNamespace(identifier="Emission Color")
    after = fingerprint(material)
    assert after is not None
    assert after != before


def test_fingerprint_ignores_node_and_link_iteration_order(fingerprint):
    material, link = _material()
    material.node_tree.links.append(SimpleNamespace(
        from_node=material.node_tree.nodes[1], from_socket=SimpleNamespace(identifier="BSDF"),
        to_node=material.node_tree.nodes[2], to_socket=SimpleNamespace(identifier="Surface")))
    before = fingerprint(material)
    assert before is not None
    material.node_tree.nodes.reverse()
    material.node_tree.links.reverse()
    assert fingerprint(material) == before
