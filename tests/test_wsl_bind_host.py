"""Coverage for the add-on's WSL_NETWORKING bind address.

Binding localhost is loopback-only, so a client in WSL cannot reach Blender on
Windows at all. 0.0.0.0 is an exposed socket, so it must stay opt-in: every
value other than an explicit yes has to keep the default.

addon.py cannot be imported without bpy, so the class is lifted out by AST.
"""

from __future__ import annotations

import ast

import pytest

from conftest import ROOT_ADDON


def _default_host(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WSL_NETWORKING", raising=False)
    else:
        monkeypatch.setenv("WSL_NETWORKING", value)

    tree = ast.parse(ROOT_ADDON.read_text())
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer"
    )
    # Keep only the pieces default_host needs; the rest of the class pulls in
    # module-level helpers that are not worth stubbing here.
    cls = ast.ClassDef(
        name=cls.name,
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[
            node for node in cls.body
            if not (isinstance(node, ast.FunctionDef) and node.name != "default_host")
        ],
        type_params=[],
    )
    ns = {"os": __import__("os")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(ROOT_ADDON), "exec"), ns)
    return ns["BlenderMCPServer"].default_host()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_opting_in_binds_all_interfaces(monkeypatch, value):
    assert _default_host(monkeypatch, value) == "0.0.0.0"


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "maybe"])
def test_everything_else_stays_on_localhost(monkeypatch, value):
    assert _default_host(monkeypatch, value) == "localhost"
