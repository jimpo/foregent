"""Shared client configuration for the foregent API server.

The CLI (:mod:`foregent.cli`) is a thin HTTP client of the API server, so the
base-URL config lives here, alongside the few other environment-overridable
settings the server needs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8577"

# Where per-issue workspaces are built (JIM-59). Outside any repo on purpose:
# a workspace is disposable and is removed when its issue completes, so it has
# no business living inside the checkout it was made from.
DEFAULT_WORKSPACE_ROOT = "~/.foregent/workspaces"

# How many agents may work at once in Pull Request mode (JIM-151). Bootstrap
# mode is one whatever this says, because it is the repo's trunk that
# serialises it rather than a policy anyone can raise.
DEFAULT_MAX_AGENTS = 3

# The levels `foregent serve` accepts (JIM-149). uvicorn also knows `trace`,
# which is left out because Python's `logging` has no such level and
# `dictConfig` rejects it.
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

DEFAULT_LOG_LEVEL = "info"


def api_url() -> str:
    """Base URL of the foregent server (``FOREGENT_API_URL`` or the default)."""
    return os.environ.get("FOREGENT_API_URL", DEFAULT_API_URL)


def workspace_root() -> Path:
    """Directory holding the per-issue workspaces (``FOREGENT_WORKSPACE_ROOT``).

    One directory per issue key is created under this, and removed when the
    issue completes (:mod:`foregent.workspaces`).
    """
    root = os.environ.get("FOREGENT_WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT
    return Path(root).expanduser()


def max_agents() -> int:
    """How many agents may run at once (``FOREGENT_MAX_AGENTS``).

    The lever an operator has over a box: every parked agent goes on holding
    its slot, so in Pull Request mode this is really the number of pull
    requests that may be open and waiting for review at once, and what one box
    and one reviewer can carry is the thing being tuned.

    **Never less than one.** A value that cannot be read, or reads as zero,
    would otherwise stop dispatch on the whole box with nothing to say why.
    """
    setting = os.environ.get("FOREGENT_MAX_AGENTS")
    if not setting:
        return DEFAULT_MAX_AGENTS
    try:
        return max(1, int(setting))
    except ValueError:
        logger.warning(
            "FOREGENT_MAX_AGENTS is %r, which is not a number; running %d agents",
            setting,
            DEFAULT_MAX_AGENTS,
        )
        return DEFAULT_MAX_AGENTS


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
   . A dev box sets nothing and works anyway.
    """
    # Empty means unset: an exported-but-blank variable is not a session name,
    # and treating it as one asks herdr for a socket nobody chose.
    return os.environ.get("FOREGENT_HERDR_SESSION") or None


def log_level() -> str:
    """Level the server logs at (``FOREGENT_LOG_LEVEL``).

    The default of ``serve``'s ``--log-level``, so the flag wins over the
    variable. The variable exists because deployment is a systemd unit, where
    every other knob is already set this way and there is no command line to
    edit.

    An unrecognised value warns and falls back rather than raising: a typo in
    a unit file should not be what stops the bridge from starting.
    """
    setting = os.environ.get("FOREGENT_LOG_LEVEL", "").strip().lower()
    if not setting:
        return DEFAULT_LOG_LEVEL
    if setting not in LOG_LEVELS:
        logger.warning(
            "FOREGENT_LOG_LEVEL is %r, which is not one of %s; logging at %s",
            setting,
            ", ".join(LOG_LEVELS),
            DEFAULT_LOG_LEVEL,
        )
        return DEFAULT_LOG_LEVEL
    return setting
