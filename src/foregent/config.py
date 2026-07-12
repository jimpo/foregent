"""Shared client configuration for the foregent API server.

The CLI (:mod:`foregent.cli`) is a thin HTTP client of the API server, so the
base-URL config lives here.
"""

from __future__ import annotations

import os

DEFAULT_API_URL = "http://127.0.0.1:8577"


def api_url() -> str:
    """Base URL of the foregent server (``FOREGENT_API_URL`` or the default)."""
    return os.environ.get("FOREGENT_API_URL", DEFAULT_API_URL)
