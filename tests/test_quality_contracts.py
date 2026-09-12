import ast
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from conftest import ROOT_ADDON


def method(name, bpy):
    tree = ast.parse(ROOT_ADDON.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'BlenderMCPServer')
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {'bpy': bpy}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<quality-contract>', 'exec'), namespace)
    return namespace[name]


def test_failed_rescue_does_not_open_checkpoint(tmp_path):
    checkpoint = tmp_path / 'saved.blend'
    checkpoint.write_bytes(b'test')
    host = SimpleNamespace(_quality_checkpoints={'saved': {'path': str(checkpoint)}},
                           create_checkpoint=Mock(side_effect=RuntimeError('disk full')))
    bpy = SimpleNamespace(ops=SimpleNamespace(wm=SimpleNamespace(open_mainfile=Mock())))
    with pytest.raises(RuntimeError, match='disk full'):
        method('restore_checkpoint', bpy)(host, 'saved')
    bpy.ops.wm.open_mainfile.assert_not_called()


def test_failed_save_does_not_register_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr('tempfile.gettempdir', lambda: str(tmp_path))
    bpy = SimpleNamespace(data=SimpleNamespace(filepath='working.blend'),
                          ops=SimpleNamespace(wm=SimpleNamespace(save_as_mainfile=Mock(return_value={'CANCELLED'}))))
    host = SimpleNamespace()
    with pytest.raises(RuntimeError, match='did not save'):
        method('create_checkpoint', bpy)(host, 'before edit')
    assert not getattr(host, '_quality_checkpoints', {})
    assert bpy.data.filepath == 'working.blend'


def test_camera_render_failure_restores_settings(tmp_path):
    camera = SimpleNamespace(type='CAMERA')
    original_camera = object()
    settings = SimpleNamespace(file_format='JPEG', color_mode='RGB')
    render = SimpleNamespace(engine='CYCLES', filepath='original.jpg', resolution_x=1920,
                             resolution_y=1080, resolution_percentage=50,
                             image_settings=settings, use_file_extension=True)
    scene = SimpleNamespace(objects={'Camera': camera}, camera=original_camera, render=render)
    bpy = SimpleNamespace(context=SimpleNamespace(scene=scene),
                          ops=SimpleNamespace(render=SimpleNamespace(render=Mock(side_effect=RuntimeError('render failed')))))
    before = dict(vars(render)), dict(vars(settings))
    with pytest.raises(RuntimeError, match='render failed'):
        method('preview_camera', bpy)(None, 'Camera', str(tmp_path / 'preview.png'))
    assert scene.camera is original_camera
    assert before == (dict(vars(render)), dict(vars(settings)))


def test_motion_preview_locks_framing_and_restores_on_failure(tmp_path):
    scene = SimpleNamespace(frame_current=9, frame_subframe=0.25, frame_set=Mock())
    bpy = SimpleNamespace(context=SimpleNamespace(scene=scene))
    preview = Mock(side_effect=[{'center': [1, 2, 3], 'radius': 4}, RuntimeError('capture failed')])
    with pytest.raises(RuntimeError, match='capture failed'):
        method('preview_animation', bpy)(SimpleNamespace(preview_target=preview), 'Cube', [1, 2], str(tmp_path))
    assert preview.call_args_list[1].args[-2:] == ([1, 2, 3], 4)
    scene.frame_set.assert_called_with(9, subframe=0.25)


def test_catalog_is_discoverable_and_exposes_effects(monkeypatch):
    monkeypatch.setenv('BLENDER_MCP_DISABLE_TELEMETRY', 'true')
    from blender_mcp import server
    catalog = server.get_quality_capabilities(None)['tools']
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert set(catalog) <= tools.keys()
    assert all(len(tools[name].description) > 80 for name in catalog)
    assert tools['sculpt_region'].annotations.destructiveHint
    assert tools['restore_checkpoint'].annotations.destructiveHint
    assert tools['inspect_scene'].annotations.readOnlyHint
    assert not tools['export_asset'].annotations.readOnlyHint
    assert 'expected_revision' in tools['sculpt_region'].inputSchema['properties']
    assert 'compare_object' in tools['validate_export'].inputSchema['properties']


def test_export_worker_has_no_inherited_mcp_stdin(monkeypatch, tmp_path):
    from blender_mcp import server
    import subprocess
    path = tmp_path / 'asset.glb'
    path.write_bytes(b'glTF')
    connection = Mock()
    connection.send_command.return_value = {'blender_executable': 'blender'}
    monkeypatch.setattr(server, 'get_blender_connection', lambda: connection)
    run = Mock(side_effect=subprocess.TimeoutExpired('blender', 90))
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='exceeded 90 seconds'):
        asyncio.run(server.validate_export(None, str(path)))
    assert run.call_args.kwargs['stdin'] is subprocess.DEVNULL
    assert run.call_args.kwargs['timeout'] == 90


@pytest.mark.parametrize('kwargs', [dict(frames=[1.5]), dict(frames=[True]),
                                      dict(frames=list(range(21))), dict(compare_object='Cube')])
def test_export_rejects_invalid_sampling_before_connecting(monkeypatch, tmp_path, kwargs):
    from blender_mcp import server
    path = tmp_path / 'asset.glb'
    path.write_bytes(b'glTF')
    connection = Mock()
    monkeypatch.setattr(server, 'get_blender_connection', connection)
    with pytest.raises(ValueError):
        asyncio.run(server.validate_export(None, str(path), **kwargs))
    connection.assert_not_called()
