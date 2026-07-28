"""The harness seam: what an agent is, independent of how it is run.

Foregent owns *what an agent is for*; an :class:`AgentManager` owns *how a
harness is driven* (``docs/PLAN.md`` §5.13). The bridge speaks only the
vocabulary in this module, so a second harness can be added without touching
dispatch.

Two constraints shape the interface, both from §9's rejected-but-supported
CAO design:

- :meth:`AgentManager.events` may be implemented as a polling loop, so no
  caller may assume a push stream exists.
- :attr:`AgentStatus.GONE` is explicit rather than inferred, so a harness
  whose own status enum has no death state can still report one.

Calls are synchronous and may block for as long as an agent takes; the API
server runs them in a threadpool, as it already does for the Linear client.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Agent labels are the bridge's own persistence scheme (docs/PLAN.md §5.11):
# an issue's agent is found again after a restart by name alone. The form is
# the lowest common denominator across harnesses — herdr requires
# `[a-z][a-z0-9_-]{0,31}`, and tmux-backed runtimes forbid "/" — so the same
# label works whoever runs the agent.
LABEL_PREFIX = "fg-"
_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class AgentError(Exception):
    """A harness operation failed.

    Managers wrap their harness-specific failures in this, so the bridge
    never catches (say) a socket error from one runtime or an HTTP error
    from another.
    """


def label_for(issue_key: str) -> str:
    """Agent label for ``issue_key`` — deterministic, so a retry collides.

    Launching twice for one issue must be impossible rather than merely
    unlikely: the second launch asks the harness for a name that is already
    taken and is refused (docs/PLAN.md §5.11).
    """
    label = f"{LABEL_PREFIX}{issue_key.lower()}"
    if not _LABEL.fullmatch(label):
        raise AgentError(
            f"issue key {issue_key!r} yields the unusable agent label {label!r}"
        )
    return label


def issue_key_from_label(label: str) -> str | None:
    """Issue key inside ``label``, or ``None`` if foregent did not launch it."""
    if not label.startswith(LABEL_PREFIX):
        return None
    key = label[len(LABEL_PREFIX) :]
    return key.upper() or None


class AgentStatus(StrEnum):
    """What an agent is doing, normalized across harnesses."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"
    # The process is gone: no pane, no session, nothing to prompt. Distinct
    # from UNKNOWN, which means "running, state not readable".
    GONE = "gone"


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything foregent decides about an agent before it starts.

    Rendered by the manager into whatever its harness wants — CLI flags for
    Claude Code, a profile file plus a REST call for CAO.
    """

    # Harness-level agent name, derived from the issue key. It is the handle
    # the bridge rebuilds its state from on boot (docs/PLAN.md §5.11), so it
    # must be deterministic per issue.
    label: str
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    model: str | None = None
    effort: str | None = None
    # Appended to the harness's own system prompt, never replacing it.
    system_prompt: str = ""
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    mcp_servers: Mapping[str, Mapping] = field(default_factory=dict)
    # Whether the agent may use only the MCP servers declared above, ignoring
    # whatever the machine is configured with. Off by default, and separate
    # from `mcp_servers` on purpose: foregent can add its own tools without
    # yet having to supply everything else an agent needs.
    strict_mcp: bool = False
    # Foregent-generated, so it is recorded before the process exists and a
    # crash between launch and first checkpoint is still recoverable.
    conversation_id: str | None = None
    # Continue `conversation_id` rather than starting it fresh.
    resume: bool = False


@dataclass(frozen=True, slots=True)
class AgentRef:
    """Handle for one agent: where it runs, and what conversation it holds.

    ``label`` locates the live process; ``conversation_id`` outlives it and
    is what a later resume needs (docs/PLAN.md §5.11, §5.12).
    """

    label: str
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """A live agent as the harness reports it, for boot reconciliation."""

    ref: AgentRef
    status: AgentStatus
    cwd: str = ""


class AgentEventKind(StrEnum):
    """Kinds of change a manager reports."""

    STATUS_CHANGED = "status_changed"
    EXITED = "exited"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Something happened to an agent.

    An ``EXITED`` event carries ``status=GONE``; its ``ref`` may lack a
    conversation id, since a dead agent is often only identifiable by label.
    """

    kind: AgentEventKind
    ref: AgentRef
    status: AgentStatus


@runtime_checkable
class AgentManager(Protocol):
    """Drives one agent harness on behalf of the bridge."""

    def launch(self, spec: LaunchSpec) -> AgentRef:
        """Start an agent for ``spec`` and return its ref, ready for input."""
        ...

    def send(self, ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
        """Deliver ``text`` to the agent.

        With ``when_idle`` the manager waits for the agent to be free first,
        which is the delivery gating a blocked-agent wake needs (§5.6).
        """
        ...

    def status(self, ref: AgentRef) -> AgentStatus:
        """Current status, or ``GONE`` if the agent no longer exists."""
        ...

    def wait(
        self,
        ref: AgentRef,
        until: Collection[AgentStatus],
        timeout: float,
    ) -> AgentStatus:
        """Block until the agent's status is in ``until``, or time out."""
        ...

    def read(self, ref: AgentRef, lines: int = 50) -> str:
        """Return the tail of the agent's output, for triage."""
        ...

    def stop(self, ref: AgentRef) -> None:
        """Tear the agent down. Stopping an absent agent is not an error."""
        ...

    def list_agents(self) -> list[AgentRecord]:
        """Every live agent this manager owns (docs/PLAN.md §5.11)."""
        ...

    def events(self) -> Iterator[AgentEvent]:
        """Yield agent changes as they happen.

        May be a push stream or a polling loop; callers treat it as an
        endless iterator and run it off the request path.
        """
        ...
