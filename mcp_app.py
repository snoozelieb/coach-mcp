"""Shared MCP application instance.

All tool modules import `mcp` from here to register their @mcp.tool() decorators.
server.py imports all tool modules to trigger registration, then runs mcp.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AI Training Coach")
