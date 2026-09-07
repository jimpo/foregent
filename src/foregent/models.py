"""Core domain types for foregent-managed issues.

An issue is claimed, worked by an agent, possibly parked while blocked on an
external event, reviewed, and completed. Every field here is written to the
state file on each change and read back at boot
(:class:`foregent.store.IssueStore`), so what outlives the process is what is
on this record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from foregent.agents import DEFAULT_PROVIDER, AgentRef, Provider


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
    # The harness the agent works this issue on, named by the operator at
    # `foregent queue`. herdr reports the agent kind too, which is what an
    # agent the state file does not know is recovered with.
    provider: Provider = DEFAULT_PROVIDER
    # The model the agent runs, named by the operator at `foregent queue`, or
    # None to let the harness choose. Used at launch only, so a queued issue
    # needs it across a restart and a running one does not.
    model: str | None = None
    blocker: str = ""
    # The issue this one was delegated from, set when a worker hands its
    # sub-issues to the queue (`queue_sub_issues`), None for an issue an
    # operator queued. The only topology foregent keeps: it decides admission
    # (a sub-issue is gated on the run limit alone) and it is who a completion
    # tells that this issue landed.
    parent: str | None = None
    # The agent working this issue: where it runs, and the conversation it
    # holds. None until dispatch. The conversation id is the half that outlives
    # the process.
    agent: AgentRef | None = None
