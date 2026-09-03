"""The shared MCP servers foregent provisions on a machine (JIM-93).

Agents reach Linear and GitHub through the box's own user-level harness
configuration rather than through their launch spec.
That makes one configuration serve two audiences — foregent's agents and the
operator's own hand-launched sessions — which is the whole reason it lives on
the machine instead of in :class:`~foregent.agents.LaunchSpec`. ``foregent
setup`` is what puts it there, so a freshly provisioned box has it without
anyone running ``claude mcp add`` or ``codex mcp add`` by hand.

**Each harness is provisioned separately**, because each keeps its own config
file and each is written through its own CLI. What is provisioned is the same
either way: the servers below, in foregent's own spelling, rendered by the
harness being written to.

Credentials are never written to disk. Both harnesses take the *name* of the
variable holding a token — a ``${LINEAR_API_KEY}`` header for Claude Code, a
``bearer_token_env_var`` for Codex — and expand it from the environment of
each session, so the token lives only in the herdr server's env and the config
files are safe to read and copy.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

from foregent.agents import DEFAULT_PROVIDER, McpServer, Provider

# What every agent, and every operator session on the box, gets to talk to.
# Foregent's own lifecycle server is not here: it is per-agent and per-run, so
# it stays in the launch spec (`server.agent_mcp_servers`).
SERVERS: dict[str, McpServer] = {
    "linear": McpServer("https://mcp.linear.app/mcp", "LINEAR_API_KEY"),
    "github": McpServer("https://api.githubcopilot.com/mcp/", "GITHUB_TOKEN"),
}

# User scope is the one that applies in every directory, which is what an
# agent in a fresh per-issue workspace needs. Codex has no scopes: what
# `codex mcp add` writes is global already.
SCOPE = "user"

DEFAULT_CODEX_HOME = "~/.codex"


class MCPError(Exception):
    """Raised when a server could not be added to the machine's config."""


def config_file(provider: Provider = DEFAULT_PROVIDER) -> Path:
    """The user-level config file ``provider`` records its servers in.

    Claude Code's is where ``--scope user`` writes. ``CLAUDE_CONFIG_DIR``
    relocates it; note that the default is ``~/.claude.json`` beside the
    config *directory*, not inside it, so this cannot be derived from
    :func:`foregent.skills.skills_root`. Codex keeps one file for everything,
    ``$CODEX_HOME/config.toml``, and has no scopes at all.
    """
    if provider is Provider.CODEX:
        home = os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME
        return Path(home).expanduser() / "config.toml"
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(config).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def configured(provider: Provider = DEFAULT_PROVIDER) -> set[str]:
    """The MCP server names ``provider`` already has on this machine.

    Read directly, but never written directly: a harness owns its config file
    and rewrites it — every running Claude Code session does — so a
    read-modify-write from here would drop whatever it recorded in between.
    Adding goes through the harness's own CLI (:func:`install`) for that
    reason.
    """
    path = config_file(provider)
    key = "mcp_servers" if provider is Provider.CODEX else "mcpServers"
    try:
        raw = path.read_bytes()
        data = (
            tomllib.loads(raw.decode())
            if provider is Provider.CODEX
            else json.loads(raw)
        )
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
        # No config yet is the normal state of a fresh box, and an unreadable
        # one is reported by the add that follows.
        return set()
    if not isinstance(data, dict):
        return set()
    servers = data.get(key)
    if not isinstance(servers, dict):
        return set()
    return {str(name) for name in servers}


def credentials(name: str) -> list[str]:
    """The environment variables ``name``'s definition expands at session start.

    Read off the definition rather than listed alongside it, so a server whose
    token changes cannot drift out of sync with the check on it.
    """
    token = SERVERS[name].token_env
    return [token] if token else []


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


def install(provider: Provider = DEFAULT_PROVIDER) -> list[tuple[str, bool]]:
    """Add every server ``provider`` is missing to the machine's config.

    ``(name, installed)`` per server, so ``setup`` can report what it did.
    Only fills gaps: an operator who authenticated a server themselves — with
    an OAuth login rather than a token — keeps that, since re-adding it would
    discard the credential foregent cannot recreate.
    """
    present = configured(provider)
    outcomes = []
    for name, server in SERVERS.items():
        installed = name not in present
        if installed:
            _add(provider, name, server)
        outcomes.append((name, installed))
    return outcomes


def _add(provider: Provider, name: str, server: McpServer) -> None:
    """Add one server through the harness's own CLI.

    Shelling out rather than editing the config keeps foregent off the exact
    shape and location of a file the harness owns and rewrites, and is the
    only writer that can be trusted not to race the sessions using it.
    """
    argv = _add_argv(provider, name, server)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MCPError(f"could not run `{' '.join(argv[:3])}`: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MCPError(f"could not add the {name} MCP server to {provider}: {detail}")


def _add_argv(provider: Provider, name: str, server: McpServer) -> list[str]:
    """The command that records one server in ``provider``'s config.

    Neither form carries the token. Claude Code stores the literal
    ``${VARIABLE}`` in a header and expands it per session; Codex is given the
    variable's name outright.
    """
    if provider is Provider.CODEX:
        argv = ["codex", "mcp", "add", name, "--url", server.url]
        if server.token_env:
            argv += ["--bearer-token-env-var", server.token_env]
        return argv
    declared: dict = {"type": "http", "url": server.url}
    if server.token_env:
        declared["headers"] = {"Authorization": f"Bearer ${{{server.token_env}}}"}
    return ["claude", "mcp", "add-json", name, json.dumps(declared), "-s", SCOPE]
