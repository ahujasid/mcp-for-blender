"""Real MCP SDK conversion/schema check; explicitly skipped without the SDK.

This does not claim a live Blender connection. The full benchmark uses STDIO MCP.
"""
import ast
import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import numpy as np
import pytest

mcp_sdk = pytest.importorskip('mcp', reason='MCP SDK is not installed; native MCP serialization unverified here')
from mcp.server.fastmcp import FastMCP, Context
from test_multiview_core import MV


def test_real_sdk_returns_image_block_and_no_structured_base64_dump():
    root = Path(__file__).resolve().parents[1]
    source = root / 'implementation/montage_server.py'
    if not source.exists():
        source = root / 'src/blender_mcp/server.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and
             n.name in {'_inspection_content', 'get_viewport_montage'}]
    png = MV._mv_png(np.full((32, 32, 4), 255, dtype=np.uint8))
    connection = Mock()
    connection.send_command.return_value = {
        'image_base64': base64.b64encode(png).decode('ascii'),
        'width': 32, 'height': 32, 'views': [], 'rows': 1, 'columns': 1,
    }
    server = FastMCP('multiview-conversion-test')
    ns = {'mcp': server, 'Context': Context, 'json': json, 'base64': base64,
          'Any': Any, 'Dict': Dict, 'List': List,
          'get_blender_connection': lambda: connection}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), ns)

    async def check():
        tools = await server.list_tools()
        tool = next(t for t in tools if t.name == 'get_viewport_montage')
        assert 'views' in tool.inputSchema['properties']
        assert 'ctx' not in tool.inputSchema['properties']
        result = await server.call_tool('get_viewport_montage', {})
        content = result[0] if isinstance(result, tuple) else result
        assert [c.type for c in content] == ['text', 'image']
        assert base64.b64decode(content[1].data) == png
        assert 'image_base64' not in content[0].text
    asyncio.run(check())
