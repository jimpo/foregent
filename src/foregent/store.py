"""In-memory store of the issues foregent is tracking.

The bridge is stateless (``docs/PLAN.md`` §5.11): the authoritative record of
live work lives in the agent harness and in Linear, and this store is only an
in-memory cache rebuilt from those backends on startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from foregent.models import Issue, IssueStatus


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

    def queue(self, key: str, directory: str) -> Issue:
        """Mark issue ``key`` Queued with ``directory``, at the back of the queue.

        Re-inserting the key makes dict insertion order the queue (FIFO)
        order, so :meth:`next_queued` needs no separate queue structure.
        Unknown keys are upserted, as in :meth:`complete`.
        """
        existing = self._issues.pop(key, None) or Issue(key=key, title="")
        issue = replace(existing, status=IssueStatus.QUEUED, directory=directory)
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

    def orphan(self, key: str) -> Issue | None:
        """Mark issue ``key`` Orphaned; its agent is gone (docs/PLAN.md §5.12).

        Unknown keys are ignored rather than upserted: an agent dying for an
        issue foregent is not tracking says nothing worth recording.
        """
        existing = self._issues.get(key)
        if existing is None:
            return None
        issue = replace(existing, status=IssueStatus.ORPHANED, agent=None)
        self._issues[key] = issue
        return issue

    def list_issues(self) -> list[Issue]:
        """Return all issues, sorted by key for stable output."""
        return sorted(self._issues.values(), key=lambda issue: issue.key)

    def __len__(self) -> int:
        return len(self._issues)

    def __iter__(self) -> Iterator[Issue]:
        return iter(self.list_issues())
