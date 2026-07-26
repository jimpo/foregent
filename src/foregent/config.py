"""Shared client configuration for the foregent API server.

The CLI (:mod:`foregent.cli`) is a thin HTTP client of the API server, so the
base-URL config lives here, alongside the few other environment-overridable
settings the server needs.
"""

from __future__ import annotations

import os

DEFAULT_API_URL = "http://127.0.0.1:8577"
DEFAULT_HERDR_SESSION = "foregent"


def api_url() -> str:
    """Base URL of the foregent server (``FOREGENT_API_URL`` or the default)."""
    return os.environ.get("FOREGENT_API_URL", DEFAULT_API_URL)


def herdr_session() -> str:
    """Name of the herdr session foregent runs its agents in.

    One named session per box, run headless as a systemd unit and attached to
    for observation (``docs/PLAN.md`` §5.10). Overridable so a test or a
    second instance never touches it.
    """
    return os.environ.get("FOREGENT_HERDR_SESSION", DEFAULT_HERDR_SESSION)
