"""Validate real STDIO MCP -> socket -> GUI Blender, without a model/API key.

Start boot_blender.py in a disposable GUI Blender first, then run:
python transport_validation.py --port 19876 --out results/transport
"""
import argparse
import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


async def validate(args):
    args.out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in {"OPENROUTER_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}}
    env.update(DISABLE_TELEMETRY="true", BLENDER_HOST="127.0.0.1",
               BLENDER_PORT=str(args.port))
    params = StdioServerParameters(command=sys.executable,
        args=["-c", "from blender_mcp.server import main; main()"], env=env)
    checks = []

    def text(result):
        return "\n".join(c.text for c in result.content if c.type == "text")

    def image(result, name, size):
        assert not result.isError, text(result)
        images = [c for c in result.content if c.type == "image"]
        assert len(images) == 1
        assert result.structuredContent is None, "Image duplicated in structured output"
        assert images[0].data not in text(result)
        raw = base64.b64decode(images[0].data, validate=True)
        with Image.open(io.BytesIO(raw)) as png:
            assert png.size == size
            png.load()
        (args.out / name).write_bytes(raw)

    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            definitions = {t.name: t for t in (await session.list_tools()).tools}
            assert "inspect" in definitions["execute_blender_code"].inputSchema["properties"]
            assert "ctx" not in definitions["get_viewport_montage"].inputSchema["properties"]
            checks.append("live schemas expose montage and optional inspection")

            code = ("bpy.ops.mesh.primitive_cube_add(size=2)\n"
                    "bpy.context.object.name = 'TransportCube'\n"
                    "bpy.context.scene['multiview_counter'] = 1\n"
                    "print('created')")
            result = await session.call_tool("execute_blender_code", {"code": code, "inspect": {}})
            assert "created" in text(result)
            image(result, "edit-and-inspect.png", (1536, 1536))
            checks.append("edit plus nine-view PNG crosses real MCP and Blender sockets")

            result = await session.call_tool("get_viewport_montage", {
                "views": ["front", {"azimuth": 42, "elevation": 23, "projection": "perspective"}],
                "target": "TransportCube", "max_size": 768,
            })
            image(result, "custom-views.png", (768, 384))
            checks.append("standalone custom-angle montage returns one native image")

            increment = "bpy.context.scene['multiview_counter'] += 1"
            result = await session.call_tool("execute_blender_code", {
                "code": increment, "inspect": {"target": ["MissingTransportTarget"]}})
            assert "edits were applied" in text(result).lower()
            assert "retry inspection only" in text(result).lower()
            result = await session.call_tool("execute_blender_code", {
                "code": increment, "inspect": {"views": []}})
            assert "error" in text(result).lower() or result.isError
            result = await session.call_tool("execute_blender_code", {
                "code": "print('counter=' + str(bpy.context.scene['multiview_counter']))"})
            assert "counter=2" in text(result), text(result)
            assert all(c.type == "text" for c in result.content)
            checks.append("failed inspection preserves one edit; malformed options prevent mutation; legacy output stays text")

    report = {"kind": "live_mcp_transport_validation", "passed": True,
              "checks": checks, "model_calls": 0}
    (args.out / "transport.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19876)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(validate(parser.parse_args()))
