"""Shared client configuration for the foregent API server.

Both the CLI (:mod:`foregent.cli`) and the MCP server
(:mod:`foregent.mcp_server`) are thin HTTP clients of the API server, so the
base-URL config lives here in one place.
"""

from __future__ import annotations

import os

DEFAULT_API_URL = "http://127.0.0.1:8577"


def api_url() -> str:
    """Base URL of the foregent server (``FOREGENT_API_URL`` or the default)."""
    return os.environ.get("FOREGENT_API_URL", DEFAULT_API_URL)
