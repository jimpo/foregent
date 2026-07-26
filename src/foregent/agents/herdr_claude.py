"""Claude Code agents, run in herdr panes (``docs/PLAN.md`` §5.13).

The manager owns every herdr and Claude Code detail: the socket calls that
open a workspace and start a process, the CLI flags a `LaunchSpec` renders
to, and the mapping from herdr's agent status onto :class:`AgentStatus`.
Nothing above it knows either name.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Collection
from dataclasses import replace

from foregent import herdr
from foregent.agents.base import (
    AgentError,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    issue_key_from_label,
)

# herdr's name for the Claude Code integration, and the detection manifest
# it loads to read that agent's state off the screen.
KIND = "claude"

# Full permissions on a dedicated box (docs/PLAN.md goal 1, §5.2). Not a
# LaunchSpec field: it is a property of how foregent runs agents at all, not
# of any one agent.
PERMISSION_MODE = "bypassPermissions"

# herdr's own budget for getting a process up and detected. Generous: a cold
# Claude Code start on a loaded box is slow, and the cost of being wrong is a
# failed dispatch.
START_MS = 90_000

# Breathing room after herdr first reports idle. The TUI is briefly
# unreceptive even once the status reads idle, and a prompt sent into that
# window is dropped with `agent_prompt_stalled`.
SETTLE_SECONDS = 2.0

# herdr error codes this manager reacts to rather than propagates.
_NOT_FOUND = "agent_not_found"
_TIMEOUT = "timeout"

# herdr's agent statuses. Anything unrecognized (a new herdr release) maps to
# UNKNOWN rather than raising: an unreadable state is not a dead agent.
_STATUS = {
    "idle": AgentStatus.IDLE,
    "working": AgentStatus.WORKING,
    "blocked": AgentStatus.BLOCKED,
    "done": AgentStatus.DONE,
    "unknown": AgentStatus.UNKNOWN,
}


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
        # --strict-mcp-config keeps the box's global MCP config out of the
        # agent, so what foregent declares is exactly what the agent gets.
        argv += [
            "--mcp-config",
            json.dumps({"mcpServers": dict(spec.mcp_servers)}),
            "--strict-mcp-config",
        ]
    # Display name in the TUI, /resume picker and terminal title — what an
    # attached operator reads to tell agents apart.
    argv += ["-n", spec.label]
    return argv


class HerdrClaudeManager:
    """Runs Claude Code agents through one herdr server."""

    def __init__(
        self,
        client: herdr.HerdrClient | None = None,
        *,
        session: str | None = None,
    ) -> None:
        self.client = client or herdr.HerdrClient(session=session)

    def launch(self, spec: LaunchSpec) -> AgentRef:
        """Open a workspace at ``spec.cwd`` and start an agent in it.

        Returns once the agent is idle and settled enough to accept a
        prompt. A conversation id is assigned here when the caller did not
        supply one, so every agent is resumable from the moment it exists.
        """
        if not spec.conversation_id:
            spec = replace(spec, conversation_id=str(uuid.uuid4()))
        workspace = self._call(
            "workspace.create",
            {
                "cwd": spec.cwd,
                "label": issue_key_from_label(spec.label) or spec.label,
                "env": dict(spec.env),
            },
        )
        pane_id = workspace["root_pane"]["pane_id"]
        workspace_id = workspace["workspace"]["workspace_id"]
        try:
            self._call(
                "agent.start",
                {
                    "name": spec.label,
                    "kind": KIND,
                    "pane_id": pane_id,
                    "args": render_args(spec),
                    "timeout_ms": START_MS,
                },
                timeout=herdr.timeout_for_wait(START_MS),
            )
            self._await_ready(spec.label)
        except AgentError:
            # Never leave a bare workspace behind: a failed dispatch that
            # leaks a pane per attempt would fill the session with debris.
            self._close_workspace(workspace_id)
            raise
        return AgentRef(spec.label, spec.conversation_id)

    def send(self, ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
        """Deliver ``text`` to the agent (JIM-86)."""
        raise NotImplementedError

    def status(self, ref: AgentRef) -> AgentStatus:
        """Current status, or ``GONE`` if herdr no longer knows the agent."""
        agent = self._agent(ref.label)
        if agent is None:
            return AgentStatus.GONE
        return _status_of(agent)

    def wait(
        self,
        ref: AgentRef,
        until: Collection[AgentStatus],
        timeout: float,
    ) -> AgentStatus:
        """Block until the agent reaches one of ``until``.

        An agent that dies while being waited on resolves the wait as
        ``GONE`` rather than hanging until the timeout — the crash authority
        of docs/PLAN.md §5.6, surfacing on the call the bridge is already
        making.
        """
        # GONE has no herdr spelling; it arrives as `agent_not_found`.
        wire = [s.value for s in until if s in _STATUS.values()]
        if not wire:
            raise AgentError(f"cannot wait for {sorted(until)}: no live status")
        timeout_ms = int(timeout * 1000)
        try:
            result = self.client.call(
                "agent.wait",
                {"target": ref.label, "until": wire, "timeout_ms": timeout_ms},
                timeout=herdr.timeout_for_wait(timeout_ms),
            )
        except herdr.HerdrAPIError as exc:
            if exc.code == _NOT_FOUND:
                return AgentStatus.GONE
            raise AgentError(f"waiting for {ref.label}: {exc}") from exc
        except herdr.HerdrError as exc:
            raise AgentError(f"waiting for {ref.label}: {exc}") from exc
        return _status_of(result.get("agent", {}))

    def read(self, ref: AgentRef, lines: int = 50) -> str:
        """Return the tail of the agent's terminal output."""
        result = self._call(
            "agent.read",
            {"target": ref.label, "source": "recent", "lines": lines},
        )
        return result.get("read", {}).get("text", "")

    def stop(self, ref: AgentRef) -> None:
        """Close the agent's whole workspace, killing its process with it."""
        agent = self._agent(ref.label)
        if agent is None:
            return
        workspace_id = agent.get("workspace_id")
        if workspace_id:
            self._close_workspace(workspace_id)
        elif agent.get("pane_id"):
            self._call("pane.close", {"pane_id": agent["pane_id"]})

    def list_agents(self) -> list[AgentRecord]:
        """Every live foregent agent, for boot reconciliation (§5.11).

        Agents herdr knows about but foregent did not launch — an operator's
        own pane in the same session — are skipped: they have no foregent
        label, so they are not ours to reconcile.
        """
        records = []
        for agent in self._call("agent.list").get("agents", []):
            label = agent.get("name")
            if not label or issue_key_from_label(label) is None:
                continue
            records.append(
                AgentRecord(
                    ref=AgentRef(label, _conversation_id_of(agent)),
                    status=_status_of(agent),
                    cwd=agent.get("cwd") or "",
                )
            )
        return records

    def _await_ready(self, label: str) -> None:
        """Wait for the agent to reach idle, then let the TUI settle."""
        try:
            self.client.call(
                "agent.wait",
                {"target": label, "until": ["idle"], "timeout_ms": START_MS},
                timeout=herdr.timeout_for_wait(START_MS),
            )
        except herdr.HerdrAPIError as exc:
            if exc.code != _TIMEOUT:
                raise AgentError(f"starting {label}: {exc}") from exc
            # A start that never reaches idle is nearly always a modal the
            # agent is sitting on — most often the workspace trust dialog
            # (docs/PLAN.md §5.8). Quote the screen so the cause is in the
            # error instead of one debugging session away.
            raise AgentError(
                f"{label} never became idle; last screen:\n"
                f"{self.read(AgentRef(label), lines=20)}"
            ) from exc
        except herdr.HerdrError as exc:
            raise AgentError(f"starting {label}: {exc}") from exc
        time.sleep(SETTLE_SECONDS)

    def _agent(self, label: str) -> dict | None:
        """herdr's record for ``label``, or ``None`` if there is no such agent."""
        try:
            return self.client.call("agent.get", {"target": label})["agent"]
        except herdr.HerdrAPIError as exc:
            if exc.code == _NOT_FOUND:
                return None
            raise AgentError(f"reading {label}: {exc}") from exc
        except herdr.HerdrError as exc:
            raise AgentError(f"reading {label}: {exc}") from exc

    def _close_workspace(self, workspace_id: str) -> None:
        """Close a workspace, tolerating one that is already gone."""
        try:
            self.client.call("workspace.close", {"workspace_id": workspace_id})
        except herdr.HerdrError:
            pass

    def _call(self, method: str, params: dict | None = None, **kwargs) -> dict:
        """Call herdr, reporting failures in the bridge's vocabulary."""
        try:
            return self.client.call(method, params, **kwargs)
        except herdr.HerdrError as exc:
            raise AgentError(str(exc)) from exc


def _status_of(agent: dict) -> AgentStatus:
    """Map a herdr agent record's status onto the bridge's enum."""
    return _STATUS.get(agent.get("agent_status", ""), AgentStatus.UNKNOWN)


def _conversation_id_of(agent: dict) -> str | None:
    """The Claude Code session id herdr learned from its integration hook.

    Present only once the agent's ``SessionStart`` hook has reported in, and
    only as a cross-check: the id foregent assigned at launch is the record
    it relies on (docs/PLAN.md §5.11).
    """
    session = agent.get("agent_session") or {}
    return session.get("value") if session.get("kind") == "id" else None
