"""look() clamps max_size on every path, including the native viewport one."""

import asyncio

from blender_mcp import server


def test_native_viewport_path_clamps_max_size(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_viewport_screenshot",
                        lambda ctx, max_size, user_prompt: seen.append(max_size)
                        or server.CallToolResult(content=[]))

    asyncio.run(server.look(None, max_size=99999))
    assert seen == [2000]

    seen.clear()
    asyncio.run(server.look(None, max_size=1))
    assert seen == [200]
