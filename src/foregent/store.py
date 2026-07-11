"""In-memory store of the issues foregent is tracking.

The bridge is stateless (``docs/PLAN.md`` §5.11): the authoritative record of
live work lives in CAO and Linear, and this store is only an in-memory cache
rebuilt from those backends. In this skeleton it starts empty and is never
populated — the rebuild path lands with the bridge.
"""

from __future__ import annotations

from collections.abc import Iterator

from foregent.models import Issue


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

    def list_issues(self) -> list[Issue]:
        """Return all issues, sorted by key for stable output."""
        return sorted(self._issues.values(), key=lambda issue: issue.key)

    def __len__(self) -> int:
        return len(self._issues)

    def __iter__(self) -> Iterator[Issue]:
        return iter(self.list_issues())
