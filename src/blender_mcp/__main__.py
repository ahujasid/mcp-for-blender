"""`python -m blender_mcp`, the same entry point as the mcp-for-blender command.

Lets Windows run the server through a signed python.exe when Smart App
Control blocks the unsigned mcp-for-blender.exe launcher (#439).
"""

from blender_mcp.server import main

main()
