"""Every tool argument that defaults to null must accept null.

A parameter written `name: str = None` advertises {"type": "string",
"default": null}, and a client that sends that default explicitly (some
agent frameworks fill every optional field with null) gets a validation
error instead of the default.
"""
import asyncio

from blender_mcp import server


def _accepts_null(prop):
    if prop.get("type") == "null":
        return True
    return any(_accepts_null(option) for option in prop.get("anyOf", []))


def test_null_defaults_accept_null():
    tools = asyncio.run(server.mcp.list_tools())
    bad = [
        f"{tool.name}.{name}"
        for tool in tools
        for name, prop in tool.inputSchema.get("properties", {}).items()
        if "default" in prop and prop["default"] is None and not _accepts_null(prop)
    ]
    assert not bad, f"default null but schema rejects null: {bad}"
