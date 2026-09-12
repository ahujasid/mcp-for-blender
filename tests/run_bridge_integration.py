import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

root = Path(__file__).resolve().parents[1]
folder = root / 'test-artifacts' / 'bridge'
folder.mkdir(parents=True, exist_ok=True)
for name in ('ready.txt', 'error.txt', 'stop.txt', 'asset.glb', 'camera.png', 'results.json'):
    (folder / name).unlink(missing_ok=True)
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
env = dict(os.environ, BLENDER_MCP_TEST_PORT=str(port), BLENDER_PORT=str(port),
           BLENDER_HOST='127.0.0.1', BLENDER_MCP_DISABLE_TELEMETRY='true',
           XDG_CONFIG_HOME=str(folder / 'config'))
if os.environ.get('BLENDER_MCP_TEST_PACKAGE'):
    env['PYTHONPATH'] = os.environ['BLENDER_MCP_TEST_PACKAGE']
startup = subprocess.STARTUPINFO() if os.name == 'nt' else None
if startup:
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
blender = os.environ.get('BLENDER_TEST_EXECUTABLE', r'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe')
log = (folder / 'blender.log').open('w', encoding='utf-8')
process = subprocess.Popen([blender, '--factory-startup', '--disable-autoexec', '--python',
                            str(root / 'tests' / 'blender_bridge_fixture.py')], env=env,
                           stdout=log, stderr=subprocess.STDOUT, startupinfo=startup)


async def run():
    params = StdioServerParameters(command=sys.executable, args=['-m', 'blender_mcp.server'], env=env)
    with (folder / 'mcp.log').open('w', encoding='utf-8') as errors:
        async with stdio_client(params, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                catalog = {tool.name: tool for tool in tools}
                expected = {'inspect_scene', 'inspect_rig', 'validate_mesh', 'inspect_motion',
                            'validate_animation', 'validate_deformation', 'inspect_sculpt',
                            'sculpt_region', 'compare_reference', 'preview_target', 'preview_animation',
                            'create_checkpoint', 'restore_checkpoint', 'export_asset', 'validate_export',
                            'preview_camera', 'get_quality_status'}
                assert expected <= catalog.keys()
                assert all(len(catalog[name].description or '') > 80 for name in expected)
                results = {}
                async def call(name, params):
                    response = await client.call_tool(name, params)
                    assert not response.isError, (name, response.content)
                    results[name] = 'passed'
                    return response
                def data(response):
                    return response.structuredContent or json.loads(response.content[0].text)
                await call('get_quality_capabilities', {})
                await call('get_quality_status', {})
                await call('inspect_scene', {'limit': 2})
                mesh = data(await call('validate_mesh', {'name': 'Cube', 'triangle_budget': 11}))
                assert mesh['counts']['triangles'] == 12 and mesh['within_triangle_budget'] is False
                await call('inspect_motion', {'name': 'Cube', 'frame_start': 1, 'frame_end': 2})
                await call('inspect_rig', {'name': 'TestRig', 'mesh_name': 'Cube'})
                await call('validate_animation', {'name': 'Cube', 'frame_start': 1, 'frame_end': 2, 'loop': True})
                attached = data(await call('validate_animation', {'name': 'HeldProp', 'frame_start': 1, 'frame_end': 2,
                                                                   'reference_object': 'TestRig', 'reference_bone': 'Bone'}))
                assert attached['warning_count'] == 0
                await call('validate_deformation', {'name': 'Cube', 'frames': [1, 2], 'reference_frame': 1})
                await call('inspect_sculpt', {'name': 'Cube'})
                checkpoint = data(await call('create_checkpoint', {'label': 'Bridge fixture'}))
                await call('sculpt_region', {'name': 'Cube', 'center': [0, 0, 0], 'radius': 3,
                                             'displacement': [0, 0, 0.1]})
                preview = await call('preview_target', {'name': 'Cube', 'view': 'three_quarter', 'max_size': 512})
                assert any(block.type == 'image' for block in preview.content)
                camera = await call('preview_camera', {'camera_name': 'Camera', 'max_size': 512,
                                                       'output_path': str(folder / 'camera.png')})
                assert any(block.type == 'image' for block in camera.content) and (folder / 'camera.png').is_file()
                motion = await call('preview_animation', {'name': 'Cube', 'frames': [1, 2]})
                assert sum(block.type == 'image' for block in motion.content) == 2
                references = root / 'test-artifacts' / 'advanced'
                comparison = await call('compare_reference', {'reference_path': str(references / 'red.png'),
                                           'preview_path': str(references / 'blue.png'),
                                           'alignment_confirmed': True, 'mode': 'overlay'})
                assert any(block.type == 'image' for block in comparison.content)
                await call('export_asset', {'names': ['Cube', 'TestRig', 'HeldProp', 'SkinnedMesh'],
                                            'filepath': str(folder / 'asset.glb'), 'frame_start': 1, 'frame_end': 2})
                exported = data(await call('validate_export', {'filepath': str(folder / 'asset.glb'), 'frames': [1, 2],
                                                                'compare_object': 'Cube'}))
                assert exported['imported'] and any(o['name'] == 'Cube' for o in exported['objects'])
                assert exported['source_comparison']['all_samples_within_tolerance']
                held = data(await call('validate_export', {'filepath': str(folder / 'asset.glb'), 'frames': [1, 2],
                                                           'compare_object': 'HeldProp', 'reference_object': 'TestRig',
                                                           'reference_bone': 'Bone'}))
                assert held['source_comparison']['all_samples_within_tolerance'], held['source_comparison']
                changed = await client.call_tool('execute_blender_code', {
                    'code': "bpy.data.objects['HeldProp'].location.x += 0.5"})
                assert not changed.isError
                different = data(await call('validate_export', {'filepath': str(folder / 'asset.glb'), 'frames': [1, 2],
                                                               'compare_object': 'HeldProp', 'reference_object': 'TestRig',
                                                               'reference_bone': 'Bone'}))
                assert not different['source_comparison']['all_samples_within_tolerance']
                await call('restore_checkpoint', {'checkpoint': checkpoint['checkpoint']})
                await call('inspect_scene', {})
                invalid = await client.call_tool('validate_mesh', {'name': 'missing'})
                assert invalid.isError
                (folder / 'results.json').write_text(json.dumps({'tools': results, 'checks': {
                    'held_prop_round_trip': held['source_comparison'],
                    'changed_source_detected': different['source_comparison'], 'fps': 60}}, indent=2), encoding='utf-8')
                print('MCP_BRIDGE_INTEGRATION_PASSED', len(results), 'tools')


try:
    deadline = time.monotonic() + 45
    while not (folder / 'ready.txt').exists():
        if (folder / 'error.txt').exists():
            raise RuntimeError((folder / 'error.txt').read_text(encoding='utf-8'))
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('Blender fixture did not become ready')
        time.sleep(0.2)
    asyncio.run(run())
finally:
    (folder / 'stop.txt').write_text('stop', encoding='utf-8')
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=10)
    log.close()
