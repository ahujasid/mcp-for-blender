"""Disposable GUI Blender process for the MCP benchmark; no personal .blend is opened."""
import argparse
import importlib.util
import os
import sys
from pathlib import Path
import bpy

ap=argparse.ArgumentParser()
ap.add_argument('--addon',type=Path,required=True)
ap.add_argument('--port',type=int,default=19876)
args=ap.parse_args(sys.argv[sys.argv.index('--')+1:])
os.environ['DISABLE_TELEMETRY']='true'
spec=importlib.util.spec_from_file_location('multiview_bench_addon',args.addon)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
module.register()

def start():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    server=module.BlenderMCPServer(host='127.0.0.1',port=args.port)
    bpy.types.blendermcp_server=server
    server.start()
    print('MULTIVIEW_BENCH_READY',flush=True)
    return None
bpy.app.timers.register(start,first_interval=1.0)
