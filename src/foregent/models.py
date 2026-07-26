"""Core domain types for foregent-managed issues.

These mirror, at a skeleton level, the lifecycle described in ``docs/PLAN.md``:
an issue is claimed, worked by a ``task_supervisor`` agent, possibly parked
while blocked on an external event, reviewed, and completed. Richer per-issue
metadata (agent/session id, workspace, typed blocker) will attach here as the
bridge lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from foregent.agents import AgentRef


class IssueStatus(StrEnum):
    """Lifecycle state of an issue as foregent sees it.

    A superset of the Linear statuses foregent reads/writes, plus the
    foregent-specific ``ORPHANED`` state (an in-flight issue whose agent is
    gone — see ``docs/PLAN.md`` §5.12).
    """

    TODO = "Todo"
    QUEUED = "Queued"
    IN_PROGRESS = "In Progress"
    BLOCKED = "Blocked"
    IN_REVIEW = "In Review"
    DONE = "Done"
    ORPHANED = "Orphaned"


@dataclass(frozen=True, slots=True)
class Issue:
    """A single unit of work foregent tracks.

    ``key`` is the Linear identifier (e.g. ``"JIM-43"``) and is the stable
    handle used throughout the system (agent labels, workspace paths).
    ``directory`` is the working directory the issue was queued with, where
    its agent is launched.
    """

    key: str
    title: str
    status: IssueStatus = IssueStatus.TODO
    directory: str = ""
    blocker: str = ""
    # The agent working this issue: where it runs, and the conversation it
    # holds. None until dispatch. The conversation id is the half that
    # outlives the process (docs/PLAN.md §5.11).
    agent: AgentRef | None = None
