"""Coverage for the WSL -> Windows screenshot path translation.

The bug: the server picks a POSIX temp path and hands it to Blender. When
Blender runs on Windows and the server in WSL, Blender resolves "/tmp/x.png"
against the current drive and writes C:\\tmp\\x.png, answering "success" while
the server finds nothing at the path it asked for.

server.py imports the mcp package and pulls in telemetry, so the helper is
lifted out by AST and executed against stubs rather than imported.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import types

from conftest import REPO_ROOT

SERVER = REPO_ROOT / "src" / "blender_mcp" / "server.py"


def _load_helper(monkeypatch, *, wslpath, run=None):
    tree = ast.parse(SERVER.read_text())
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_blender_side_path"
    )
    ns = {
        "os": __import__("os"),
        "shutil": types.SimpleNamespace(which=lambda name: "/usr/bin/wslpath" if wslpath else None),
        "subprocess": types.SimpleNamespace(
            run=run or (lambda *a, **k: None),
            CalledProcessError=subprocess.CalledProcessError,
        ),
        "DEFAULT_HOST": "localhost",
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SERVER), "exec"), ns)
    return ns["_blender_side_path"]


def _wslpath(returncode=0, stdout="\\\\wsl.localhost\\Ubuntu\\tmp\\shot.png"):
    def run(argv, **kwargs):
        assert argv[:2] == ["wslpath", "-w"]
        if returncode:
            raise subprocess.CalledProcessError(returncode, argv)
        return types.SimpleNamespace(stdout=stdout + "\n")
    return run


def test_remote_host_in_wsl_gets_a_unc_path(monkeypatch):
    monkeypatch.setenv("BLENDER_HOST", "172.25.112.1")
    helper = _load_helper(monkeypatch, wslpath=True, run=_wslpath())
    assert helper("/tmp/shot.png") == "\\\\wsl.localhost\\Ubuntu\\tmp\\shot.png"


def test_local_host_is_left_alone(monkeypatch):
    """Blender on the same machine shares the filesystem - never translate."""
    monkeypatch.setenv("BLENDER_HOST", "localhost")
    helper = _load_helper(monkeypatch, wslpath=True, run=_wslpath())
    assert helper("/tmp/shot.png") == "/tmp/shot.png"


def test_unset_host_is_left_alone(monkeypatch):
    monkeypatch.delenv("BLENDER_HOST", raising=False)
    helper = _load_helper(monkeypatch, wslpath=True, run=_wslpath())
    assert helper("/tmp/shot.png") == "/tmp/shot.png"


def test_remote_host_without_wsl_is_left_alone(monkeypatch):
    """A remote Linux Blender reached over the network still wants POSIX."""
    monkeypatch.setenv("BLENDER_HOST", "192.168.1.50")
    helper = _load_helper(monkeypatch, wslpath=False)
    assert helper("/tmp/shot.png") == "/tmp/shot.png"


def test_wslpath_failure_falls_back_to_the_original(monkeypatch):
    monkeypatch.setenv("BLENDER_HOST", "172.25.112.1")
    helper = _load_helper(monkeypatch, wslpath=True, run=_wslpath(returncode=1))
    assert helper("/tmp/shot.png") == "/tmp/shot.png"


def test_empty_wslpath_output_falls_back_to_the_original(monkeypatch):
    monkeypatch.setenv("BLENDER_HOST", "172.25.112.1")
    helper = _load_helper(monkeypatch, wslpath=True, run=_wslpath(stdout=""))
    assert helper("/tmp/shot.png") == "/tmp/shot.png"
