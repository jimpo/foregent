"""Claude Code: herdr's name for it, and the flags a launch spec renders to.

Everything in here is Claude Code's own vocabulary. It is reached through
:mod:`foregent.agents.harness`, so nothing that decides *what* an agent is for
has to know which harness will run it.
"""

from __future__ import annotations

import json

from foregent.agents.base import LaunchSpec

# herdr's name for the Claude Code integration, and the detection manifest it
# loads to read that agent's state off the screen.
KIND = "claude"

# Full permissions on a dedicated box. Not a LaunchSpec field: it is a property
# of how foregent runs agents at all, not of any one agent.
PERMISSION_MODE = "bypassPermissions"


def render_args(spec: LaunchSpec) -> list[str]:
    """The ``claude`` flags a ``LaunchSpec`` asks for.

    The binary itself is herdr's to supply — ``agent.start`` prepends it
    from the agent kind's manifest.
    """
    argv: list[str] = []
    if spec.conversation_id:
        # --resume continues the recorded conversation; --session-id names a
        # new one. Passing both is a contradiction, so they are exclusive.
        argv += (
            ["--resume", spec.conversation_id]
            if spec.resume
            else ["--session-id", spec.conversation_id]
        )
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    argv += ["--permission-mode", PERMISSION_MODE]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    if spec.tools_allow:
        argv += ["--allowedTools", *spec.tools_allow]
    if spec.tools_deny:
        argv += ["--disallowedTools", *spec.tools_deny]
    if spec.mcp_servers:
        argv += ["--mcp-config", json.dumps({"mcpServers": dict(spec.mcp_servers)})]
    if spec.strict_mcp:
        # Independent of the declaration above: this one says to ignore
        # whatever MCP config the machine already has, so what foregent
        # declares is exactly what the agent gets.
        argv += ["--strict-mcp-config"]
    # Display name in the TUI, /resume picker and terminal title — what an
    # attached operator reads to tell agents apart.
    argv += ["-n", spec.label]
    return argv
