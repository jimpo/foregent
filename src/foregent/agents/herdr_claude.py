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
from collections.abc import Collection, Generator, Iterator
from dataclasses import replace

from foregent import herdr
from foregent.agents.base import (
    AgentError,
    AgentEvent,
    AgentEventKind,
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

# An agent reads as idle before its TUI can accept input; herdr exposes the
# real precondition as `interactive_ready`, and prompting early is refused
# with `agent_not_ready`. Poll for it rather than sleeping a guessed amount.
READY_TIMEOUT = 30
POLL_SECONDS = 0.25

# Statuses in which an agent is free to be given something new to do.
READY = frozenset({AgentStatus.IDLE, AgentStatus.DONE})

# How long `send` waits for a busy agent to come free before giving up.
IDLE_TIMEOUT = 300

# A prompt is retried only after checking whether it actually landed; three
# attempts is enough for a transient TUI hiccup and small enough that a
# genuinely wedged agent surfaces quickly.
PROMPT_ATTEMPTS = 3
RETRY_SECONDS = 2.0

# Budget for the delivery check herdr performs on a prompt. It must exceed
# herdr's own five-second stall window, or the wait expires before the check
# it exists to run can report.
CONFIRM_MS = 15_000

# The events every subscription carries, whatever agents exist: the three
# ways an agent can end — which are the bridge's crash authority
# (docs/PLAN.md §5.6) — plus the arrival of a new one.
#
# `workspace.closed` is not redundant. Stopping an agent closes its whole
# workspace, and that emits only `workspace_closed`; no pane event follows.
GLOBAL_SUBSCRIPTIONS = [
    {"type": "pane.exited"},
    {"type": "pane.closed"},
    {"type": "workspace.closed"},
    {"type": "pane.agent_detected"},
]

# Pause before re-subscribing after the stream drops. A missed window means
# missed deaths, so this is short.
RECONNECT_SECONDS = 2.0

# How often a quiet subscription checks whether the fleet has changed. Status
# is watched per pane, so an agent that started since the subscription opened
# is invisible until it is re-subscribed — and herdr's `pane_agent_detected`
# can arrive before the agent has a name, so it cannot be the only trigger.
# The consequence is a short window after a launch in which that agent's
# status changes are not reported; its death still is, since those
# subscriptions are global. `agent.list` over a unix socket is cheap enough
# to run this often.
TICK_SECONDS = 2.0

# herdr error codes this manager reacts to rather than propagates.
_NOT_FOUND = "agent_not_found"
_TIMEOUT = "timeout"
_NOT_READY = "agent_not_ready"
_STALLED = "agent_prompt_stalled"

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
        """Deliver ``text`` to the agent, retrying only if it did not land.

        This is both the assignment brief at dispatch and the wake of a
        parked agent (docs/PLAN.md §5.6), so silently dropping a message and
        silently sending it twice are both real failures.
        """
        if when_idle:
            status = self.wait(ref, READY, IDLE_TIMEOUT)
            if status is AgentStatus.GONE:
                raise AgentError(f"cannot send to {ref.label}: agent is gone")
        self._await_interactive(ref.label)
        self._prompt(ref, text)

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
        # Sorted so the same wait always produces the same request.
        wire = sorted(s.value for s in until if s in _STATUS.values())
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

    def _prompt(self, ref: AgentRef, text: str) -> None:
        """Submit ``text``, and only call it sent once the agent reacted.

        The prompt carries a ``wait``, which is what makes herdr watch for a
        lifecycle change and answer `agent_prompt_stalled` when none comes.
        That check is the delivery oracle, and it is not optional: a bare
        prompt is reported as succeeding even when the text is swallowed by a
        modal the agent is sitting on — the workspace trust dialog on any
        directory Claude Code has not been told to trust — leaving the
        message in the input box, unsent, while herdr still reports the agent
        idle and interactive.

        Retrying is therefore safe: a stall means the agent never saw it.
        Retrying *without* the check would be how a woken agent answers the
        same message twice (docs/PLAN.md §5.6, §5.13).
        """
        for attempt in range(PROMPT_ATTEMPTS):
            try:
                self.client.call(
                    "agent.prompt",
                    {
                        "target": ref.label,
                        "text": text,
                        "wait": {"until": ["working"], "timeout_ms": CONFIRM_MS},
                    },
                    timeout=herdr.timeout_for_wait(CONFIRM_MS),
                )
                return
            except herdr.HerdrAPIError as exc:
                if exc.code == _TIMEOUT:
                    # The stall check passed — the agent reacted — it just
                    # never entered `working` while we watched. Delivered.
                    return
                if exc.code not in (_STALLED, _NOT_READY):
                    raise AgentError(f"prompting {ref.label}: {exc}") from exc
            except herdr.HerdrError as exc:
                raise AgentError(f"prompting {ref.label}: {exc}") from exc
            if attempt < PROMPT_ATTEMPTS - 1:
                time.sleep(RETRY_SECONDS)
        raise AgentError(
            f"could not deliver a prompt to {ref.label} in {PROMPT_ATTEMPTS} "
            f"attempts; last screen:\n{self.read(ref, lines=20)}"
        )

    def events(self) -> Iterator[AgentEvent]:
        """Yield agent changes, re-subscribing for as long as it is consumed.

        The stream dropping is not an ending: a bridge that stopped listening
        would stop noticing agents dying, so this reconnects and keeps going.
        Events name panes, not agents, so labels are resolved through a map
        rebuilt on subscribe and whenever an unfamiliar pane appears.
        """
        while True:
            immediate = False
            try:
                immediate = yield from self._events_once()
            except (herdr.HerdrError, AgentError):
                pass
            if not immediate:
                time.sleep(RECONNECT_SECONDS)

    def _events_once(self) -> Generator[AgentEvent, None, bool]:
        """One subscription's worth of events; True to re-subscribe at once.

        Status changes are per-pane subscriptions, which is the only place
        they can be read reliably: the global `pane.updated` carries a
        `PaneInfo` whose `agent_status` lags — it reported an agent idle
        while it was working — because it describes the pane, not the agent.
        The cost is that a pane arriving later needs a new subscription,
        which is what the return value asks for.
        """
        panes, workspaces = self._agent_labels()
        subscriptions = GLOBAL_SUBSCRIPTIONS + [
            {"type": "pane.agent_status_changed", "pane_id": pane_id}
            for pane_id in panes
        ]
        seen: dict[str, AgentStatus] = {}
        for message in self.client.subscribe(subscriptions, tick=TICK_SECONDS):
            if message is None:
                # A quiet moment: pick up agents that appeared since this
                # subscription opened, whose status nothing is watching yet.
                live, _ = self._agent_labels()
                if set(live) != set(panes):
                    return True
                continue
            # herdr is inconsistent about how it spells event names on the
            # wire: most arrive underscored, as the schema's EventKind lists
            # them, but a per-pane status change arrives as the dotted
            # subscription type (`pane.agent_status_changed`). Normalizing
            # means a spelling change cannot silently drop events — which is
            # exactly how status updates were lost before.
            kind = str(message.get("event", "")).replace(".", "_")
            data = message.get("data") or {}
            if kind == "pane_agent_status_changed":
                pane_id = data.get("pane_id")
                label = panes.get(pane_id) if pane_id else None
                if label is None or pane_id is None:
                    continue
                status = _status_of(data)
                if seen.get(pane_id) == status:
                    continue
                seen[pane_id] = status
                yield AgentEvent(
                    AgentEventKind.STATUS_CHANGED, AgentRef(label), status
                )
            elif kind in ("pane_exited", "pane_closed"):
                pane_id = data.get("pane_id")
                label = panes.pop(pane_id, None) if pane_id else None
                seen.pop(pane_id, None)
                if label is not None:
                    yield AgentEvent(
                        AgentEventKind.EXITED, AgentRef(label), AgentStatus.GONE
                    )
            elif kind == "workspace_closed":
                workspace_id = data.get("workspace_id")
                label = workspaces.pop(workspace_id, None) if workspace_id else None
                if label is not None:
                    yield AgentEvent(
                        AgentEventKind.EXITED, AgentRef(label), AgentStatus.GONE
                    )
            elif kind == "pane_agent_detected":
                pane_id = data.get("pane_id")
                if pane_id is None or pane_id in panes:
                    continue
                # Someone else's agent in this session is not worth a new
                # subscription; ours needs one, and only a fresh subscription
                # can carry a per-pane entry.
                #
                # `panes` is deliberately left alone unless we resubscribe:
                # recording an agent here that nothing is yet subscribed to
                # would make the tick below see no change and never catch up,
                # silently losing that agent's status for its whole life.
                if pane_id in self._agent_labels()[0]:
                    return True
        return False

    def _agent_labels(self) -> tuple[dict[str, str], dict[str, str]]:
        """Agent labels keyed by pane and by workspace.

        Events name panes and workspaces; the bridge speaks in labels, and
        only `agent.list` joins the two.
        """
        panes: dict[str, str] = {}
        workspaces: dict[str, str] = {}
        for agent in self._call("agent.list").get("agents", []):
            label = agent.get("name")
            if not label or issue_key_from_label(label) is None:
                continue
            if agent.get("pane_id"):
                panes[agent["pane_id"]] = label
            if agent.get("workspace_id"):
                workspaces[agent["workspace_id"]] = label
        return panes, workspaces

    def _await_interactive(self, label: str) -> None:
        """Block until herdr reports the agent's TUI can accept input."""
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            agent = self._agent(label)
            if agent is None:
                raise AgentError(f"{label} is gone")
            if agent.get("interactive_ready"):
                return
            time.sleep(POLL_SECONDS)
        raise AgentError(
            f"{label} never became interactive; last screen:\n"
            f"{self.read(AgentRef(label), lines=20)}"
        )

    def _await_ready(self, label: str) -> None:
        """Wait for the agent to reach idle and be able to take input."""
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
        self._await_interactive(label)

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
