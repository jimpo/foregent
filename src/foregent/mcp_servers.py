"""The shared MCP servers foregent provisions on a machine (JIM-93).

Agents reach Linear and GitHub through the box's own user-level Claude Code
configuration rather than through their launch spec.
That makes one configuration serve two audiences — foregent's agents and the
operator's own hand-launched sessions — which is the whole reason it lives on
the machine instead of in :class:`~foregent.agents.LaunchSpec`. ``foregent
setup`` is what puts it there, so a freshly provisioned box has it without
anyone running ``claude mcp add`` by hand.

Credentials are never written to disk. The stored header holds the literal
``${LINEAR_API_KEY}``; Claude Code expands it from the environment of each
session, so the token lives only in the herdr server's env and the
config file is safe to read and copy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# What every agent, and every operator session on the box, gets to talk to.
# Foregent's own lifecycle server is not here: it is per-agent and per-run, so
# it stays in the launch spec (`server.agent_mcp_servers`).
SERVERS: dict[str, dict] = {
    "linear": {
        "type": "http",
        "url": "https://mcp.linear.app/mcp",
        "headers": {"Authorization": "Bearer ${LINEAR_API_KEY}"},
    },
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
    },
}

# User scope is the one that applies in every directory, which is what an
# agent in a fresh per-issue workspace needs.
SCOPE = "user"


class MCPError(Exception):
    """Raised when a server could not be added to the machine's config."""


def config_file() -> Path:
    """Claude Code's user-level config file, where ``--scope user`` writes.

    ``CLAUDE_CONFIG_DIR`` relocates it; note that the default is ``~/.claude
    .json`` beside the config *directory*, not inside it, so this cannot be
    derived from :func:`foregent.skills.skills_root`.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(config).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def configured() -> set[str]:
    """The user-scope MCP server names already on this machine.

    Read directly, but never written directly: every running Claude Code
    session rewrites this file, so a read-modify-write from here would drop
    whatever a session recorded in between. Adding goes through ``claude mcp``
    (:func:`install`) for that reason.
    """
    try:
        data = json.loads(config_file().read_text())
    except (OSError, ValueError):
        # No config yet is the normal state of a fresh box, and an unreadable
        # one is reported by the add that follows.
        return set()
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    return {str(name) for name in servers}


def credentials(name: str) -> list[str]:
    """The environment variables ``name``'s definition expands at session start.

    Derived from the definition rather than listed alongside it, so a server
    whose header changes cannot drift out of sync with the check on it.
    """
    return re.findall(r"\$\{(\w+)\}", json.dumps(SERVERS[name]))


def missing_credentials() -> list[str]:
    """Every credential a configured server needs that this environment lacks.

    A server whose variable is unset is worse than an absent one: it is
    configured, looks installed, and fails to authenticate at run time.
    """
    return sorted(
        {
            variable
            for name in SERVERS
            for variable in credentials(name)
            if not os.environ.get(variable)
        }
    )


def install() -> list[tuple[str, bool]]:
    """Add every missing server to the machine's user config.

    ``(name, installed)`` per server, so ``setup`` can report what it did.
    Only fills gaps: an operator who authenticated a server themselves — with
    an OAuth login rather than a token — keeps that, since re-adding it would
    discard the credential foregent cannot recreate.
    """
    present = configured()
    outcomes = []
    for name, definition in SERVERS.items():
        installed = name not in present
        if installed:
            _add(name, definition)
        outcomes.append((name, installed))
    return outcomes


def _add(name: str, definition: dict) -> None:
    """Add one server through Claude Code's own CLI.

    Shelling out rather than editing the config keeps foregent off the exact
    shape and location of a file Claude Code owns and rewrites, and is the
    only writer that can be trusted not to race the sessions using it.
    """
    try:
        result = subprocess.run(
            ["claude", "mcp", "add-json", name, json.dumps(definition), "-s", SCOPE],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPError(f"could not run `claude mcp add-json {name}`: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MCPError(f"could not add the {name} MCP server: {detail}")
