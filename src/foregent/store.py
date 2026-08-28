"""In-memory store of the issues foregent is tracking.

The bridge is stateless: the authoritative record of
live work lives in the agent harness and in Linear, and this store is only an
in-memory cache rebuilt from those backends on startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from foregent.models import Issue, IssueStatus

# An issue with a live agent working it, whether or not that agent is busy.
# These are the states an event can be delivered into and the ones an issue can
# be orphaned out of.
IN_FLIGHT = (IssueStatus.IN_PROGRESS, IssueStatus.IN_REVIEW, IssueStatus.BLOCKED)


class IssueStore:
    """A mutable, in-memory collection of issues keyed by issue key.

    Starts empty. Not thread-safe; the bridge will own synchronization when it
    drives concurrent updates.
    """

    def __init__(self) -> None:
        self._issues: dict[str, Issue] = {}

    def add(self, issue: Issue) -> None:
        """Insert or replace an issue by its key."""
        self._issues[issue.key] = issue

    def get(self, key: str) -> Issue | None:
        """Return the issue with ``key``, or ``None`` if absent."""
        return self._issues.get(key)

    def queue(self, key: str, repo: str) -> Issue:
        """Mark issue ``key`` Queued against ``repo``, at the back of the queue.

        Re-inserting the key makes dict insertion order the queue (FIFO)
        order, so :meth:`next_queued` needs no separate queue structure.
        Unknown keys are upserted, as in :meth:`complete`.

        Only the repo is known here. The agent's own directory is the
        workspace dispatch builds from it, so it is set there.
        """
        existing = self._issues.pop(key, None) or Issue(key=key, title="")
        issue = replace(existing, status=IssueStatus.QUEUED, repo=repo, directory="")
        self._issues[key] = issue
        return issue

    def next_queued(self) -> Issue | None:
        """Return the oldest Queued issue, or ``None`` if the queue is empty."""
        return next(
            (i for i in self._issues.values() if i.status is IssueStatus.QUEUED),
            None,
        )

    def complete(self, key: str) -> Issue:
        """Mark the issue ``key`` as Done, upserting a minimal issue if unknown.

        There is no dispatch/claim path yet (the store starts empty), so an
        unknown key is created rather than rejected.
        """
        existing = self._issues.get(key)
        issue = (
            replace(existing, status=IssueStatus.DONE)
            if existing is not None
            else Issue(key=key, title="", status=IssueStatus.DONE)
        )
        self._issues[key] = issue
        return issue

    def block(self, key: str, blocker: str) -> Issue:
        """Mark the issue ``key`` as Blocked with ``blocker``, upserting if unknown.

        Mirrors :meth:`complete`: an unknown key is created rather than
        rejected, since there is no dispatch/claim path yet.
        """
        existing = self._issues.get(key)
        issue = (
            replace(existing, status=IssueStatus.BLOCKED, blocker=blocker)
            if existing is not None
            else Issue(key=key, title="", status=IssueStatus.BLOCKED, blocker=blocker)
        )
        self._issues[key] = issue
        return issue

    def unblock(self, key: str) -> Issue | None:
        """Return issue ``key`` to In Progress, clearing its blocker.

        The counterpart to :meth:`block`: the event the agent was parked on
        has arrived, and its still-live process is about to be prompted with
        it. Capacity does not change, because a parked
        agent was holding its slot the whole time.

        Only a BLOCKED issue can be unblocked; everything else returns
        ``None`` and is left alone, in the shape of :meth:`orphan`'s guard.
        Waking an issue that was never parked is not a state to correct — it
        is a message with nowhere to go, and the caller answers for it.
        """
        existing = self._issues.get(key)
        if existing is None or existing.status is not IssueStatus.BLOCKED:
            return None
        issue = replace(existing, status=IssueStatus.IN_PROGRESS, blocker="")
        self._issues[key] = issue
        return issue

    def orphan(self, key: str) -> Issue | None:
        """Mark issue ``key`` Orphaned; its agent is gone.

        Only an *in-flight* issue can be orphaned. Everything else returns
        ``None`` and is left alone:

        - Unknown keys are ignored rather than upserted: an agent dying for an
          issue foregent is not tracking says nothing worth recording.
        - Done is not overwritten. Foregent stops an agent itself once its
          issue completes, and the harness reports that deliberate teardown as
          the same event as a crash; the issue's own status is the only thing
          that tells them apart, and ``complete()`` has already run by then.
        """
        existing = self._issues.get(key)
        if existing is None or existing.status not in IN_FLIGHT:
            return None
        issue = replace(existing, status=IssueStatus.ORPHANED, agent=None)
        self._issues[key] = issue
        return issue

    def in_flight(self) -> list[Issue]:
        """Every issue with a live agent working it, busy or parked.

        What the event tick polls Linear about: activity
        only matters on issues foregent has an agent for, and only those cost
        anything to watch.
        """
        return [i for i in self.list_issues() if i.status in IN_FLIGHT]

    def list_issues(self) -> list[Issue]:
        """Return all issues, sorted by key for stable output."""
        return sorted(self._issues.values(), key=lambda issue: issue.key)

    def __len__(self) -> int:
        return len(self._issues)

    def __iter__(self) -> Iterator[Issue]:
        return iter(self.list_issues())
