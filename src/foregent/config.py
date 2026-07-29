"""Shared client configuration for the foregent API server.

The CLI (:mod:`foregent.cli`) is a thin HTTP client of the API server, so the
base-URL config lives here, alongside the few other environment-overridable
settings the server needs.
"""

from __future__ import annotations

import os

DEFAULT_API_URL = "http://127.0.0.1:8577"

# Seconds between event polls. Linear allows 2,500 requests an hour, so one
# query every 30 seconds spends under 5% of the budget; every consumer today
# is a human replying to a review, who does not feel the difference (JIM-102).
DEFAULT_POLL_INTERVAL = 30.0


def api_url() -> str:
    """Base URL of the foregent server (``FOREGENT_API_URL`` or the default)."""
    return os.environ.get("FOREGENT_API_URL", DEFAULT_API_URL)


def poll_interval() -> float:
    """Seconds between event polls (``FOREGENT_POLL_INTERVAL`` or the default).

    An unreadable value falls back to the default rather than stopping the
    bridge: a typo in a unit file is not worth losing event delivery over, and
    the tick logs the interval it settled on when it starts.
    """
    try:
        return float(os.environ["FOREGENT_POLL_INTERVAL"])
    except (KeyError, ValueError):
        return DEFAULT_POLL_INTERVAL


def herdr_session() -> str | None:
    """Name of the herdr session foregent runs its agents in, if pinned.

    ``FOREGENT_HERDR_SESSION`` names one outright; ``None`` hands the choice
    to the client, which then takes the session this process was started in —
    herdr injects ``HERDR_SOCKET_PATH`` into every pane it owns, so a bridge
    launched from a pane inherits its own session — and falls back to herdr's
    default session (:func:`foregent.herdr.socket_path`).

    Deployment pins it, and the ordering is why. The systemd unit runs outside
    any herdr pane, so with only the fallbacks it would land in the *default*
    session, putting foregent's agents in the operator's interactive one
    instead of the dedicated session that exists to be attached to read-only
    (``docs/PLAN.md`` §5.10). A dev box sets nothing and works anyway.
    """
    # Empty means unset: an exported-but-blank variable is not a session name,
    # and treating it as one asks herdr for a socket nobody chose.
    return os.environ.get("FOREGENT_HERDR_SESSION") or None
