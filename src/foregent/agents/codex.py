"""Codex: herdr's name for it, and the flags a launch spec renders to.

Everything in here is Codex's own vocabulary. It is reached through
:mod:`foregent.agents.harness`, so nothing that decides *what* an agent is for
has to know which harness will run it.

Two things Claude Code offers have no Codex spelling, and are refused rather
than dropped: an appended system prompt, and tool allow and deny lists.
Nothing in foregent asks for either, and an agent launched quietly without a
restriction its caller asked for is the failure nobody notices.
"""

from __future__ import annotations

from foregent.agents.base import AgentError, LaunchSpec, McpServer

# herdr's name for the Codex integration, and the detection manifest it loads
# to read that agent's state off the screen.
KIND = "codex"

# Full permissions on a dedicated box, the counterpart of Claude Code's
# `--permission-mode bypassPermissions`. Not a LaunchSpec field: it is a
# property of how foregent runs agents at all, not of any one agent.
BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def render_args(spec: LaunchSpec) -> list[str]:
    """The ``codex`` arguments a ``LaunchSpec`` asks for.

    The binary itself is herdr's to supply — ``agent.start`` prepends it from
    the agent kind's manifest.

    **A fresh Codex conversation cannot be named in advance.** Codex has no
    counterpart of ``--session-id``; it records a session of its own and
    ``resume`` takes that id afterwards. A conversation id foregent generated
    is therefore unused here, and the one worth keeping is what
    :meth:`~foregent.agents.herdr_manager.HerdrManager.launch` reads back off
    herdr once the agent has started.
    """
    argv: list[str] = []
    if spec.resume:
        if not spec.conversation_id:
            raise AgentError("cannot resume a Codex agent without a session id")
        # A subcommand, not a flag, so it leads; its options are the same
        # ones a fresh session takes.
        argv += ["resume", spec.conversation_id]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["-c", f"model_reasoning_effort={spec.effort}"]
    argv += [BYPASS]
    if spec.system_prompt:
        raise AgentError("Codex cannot append to its system prompt")
    if spec.tools_allow or spec.tools_deny:
        raise AgentError("Codex has no tool allow or deny list")
    for name, server in spec.mcp_servers.items():
        argv += _server(name, server)
    if spec.strict_mcp:
        # `-c` overrides merge onto the machine's config rather than replacing
        # it, established by driving codex 0.153: emptying `mcp_servers` and
        # then naming one leaves every machine-level server in place. There is
        # no argument that means "only these", so this cannot be honored.
        raise AgentError("Codex cannot be restricted to the servers foregent declares")
    return argv


def _server(name: str, server: McpServer) -> list[str]:
    """One MCP server as Codex config overrides, a leaf at a time.

    ``-c`` parses its value as TOML and falls back to a literal string, so a
    URL needs no quoting of foregent's own and no TOML is written by hand.
    The override merges over whatever the machine already declares, which is
    what lets the per-run foregent server join the box's Linear and GitHub.
    """
    argv = ["-c", f"mcp_servers.{name}.url={server.url}"]
    if server.token_env:
        # Named, never carried: Codex reads the variable out of the session's
        # own environment, so the token reaches no file.
        argv += ["-c", f"mcp_servers.{name}.bearer_token_env_var={server.token_env}"]
    return argv


BRIEF = (
    "Follow the foregent-worker skill. Your Linear issue is {key}, "
    "and the project lands work in {mode} mode."
)
"""The opening message a Codex agent is given.

Codex has no slash form for a skill. It lists every skill it has found by name
and description in the model's own prompt, so naming the skill is what makes
an agent read it, and the lifecycle still has one definition — the skill —
rather than half of one here.
"""


def brief(key: str, mode: str) -> str:
    """The opening message for issue ``key`` in ``mode``."""
    return BRIEF.format(key=key, mode=mode)
