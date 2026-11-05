"""Local (stdio) server: signs in once with `login`, then runs on your own computer."""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .tools import register

mcp = FastMCP("onedrive-personal")
register(mcp, readonly=os.environ.get("ONEDRIVE_READONLY") == "1")


def run() -> None:
    mcp.run()
