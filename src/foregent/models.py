"""Core domain types for foregent-managed issues.

An issue is claimed, worked by an agent, possibly parked while blocked on an
external event, reviewed, and completed. The per-issue metadata that must
outlive a process will attach here as the durable Linear-side store lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from foregent.agents import AgentRef


class Mode(StrEnum):
    """How a project wants its work landed.

    Derived from the repo's git remotes rather than declared anywhere
    (:func:`foregent.workspaces.mode_for`), and named to the agent in its
    brief, so the two halves of the contract cannot disagree.
    """

    # The agent commits on top of `main` and stops there; the bridge moves the
    # bookmark when the issue completes.
    BOOTSTRAP = "bootstrap"
    # The agent pushes a branch, opens a pull request, and parks on the review.
    PULL_REQUEST = "pull-request"


class IssueStatus(StrEnum):
    """Lifecycle state of an issue as foregent sees it.

    A superset of the Linear statuses foregent reads/writes, plus the
    foregent-specific ``ORPHANED`` state (an in-flight issue whose agent is
    gone).
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

    ``repo`` is the project directory the issue was queued with, and
    ``directory`` is where its agent actually runs: the per-issue workspace
    built from ``repo`` at dispatch (:mod:`foregent.workspaces`), or ``repo``
    itself where foregent cannot make one. Both are kept, because teardown
    needs the repo to forget the workspace and the path to remove it.
    """

    key: str
    title: str
    status: IssueStatus = IssueStatus.TODO
    repo: str = ""
    directory: str = ""
    blocker: str = ""
    # The agent working this issue: where it runs, and the conversation it
    # holds. None until dispatch. The conversation id is the half that outlives
    # the process.
    agent: AgentRef | None = None
