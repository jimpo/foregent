"""The store of the issues foregent is tracking, and the file it lives in.

Three parties hold state, and the split is by what each can vouch for. Linear
holds issue truth: ownership and status as the world sees them. The agent
harness holds liveness: which agents exist and what each is doing. This store
holds **foregent's own intent** — queue order, which agent was bound to which
issue, what a parked agent said it was waiting for — which nothing else can
rebuild, because nothing else was told. It is snapshotted to one JSON file on
every write and read back at boot, where it is reconciled against the harness
before anything trusts it (:func:`foregent.server.rebuild_store`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from foregent.agents import DEFAULT_PROVIDER, AgentRef, Provider
from foregent.models import Issue, IssueStatus

logger = logging.getLogger(__name__)

# The shape of the state file. A file of any other version is not read: there
# is no migration until there is a second version to migrate from, and
# starting empty is exactly the position the bridge was in before it had a
# file at all.
STATE_VERSION = 1

# An issue with a live agent working it, whether or not that agent is busy.
# These are the states an event can be delivered into and the ones an issue can
# be orphaned out of.
IN_FLIGHT = (IssueStatus.IN_PROGRESS, IssueStatus.IN_REVIEW, IssueStatus.BLOCKED)


class IssueStore:
    """A mutable collection of issues keyed by issue key, mirrored to a file.

    Given a ``path``, the store loads it on construction and rewrites it on
    every change; given none, it is in memory only, which is what tests want.
    Insertion order is the queue order, and the file keeps it.

    **Each method is atomic**, because the bridge reaches this store from
    several threads at once — the delivery drainers, the harness event
    watcher, the MCP tools and the HTTP routes — and a read-modify-write split
    across two of them loses one of the writes. The save runs under the same
    lock, so the file is always some complete state the store was in.

    A sequence of calls is not atomic, and this lock does not pretend
    otherwise: a caller that reads the store and then writes what it read owns
    that race itself (:func:`foregent.server.dispatch` is the one that
    matters, and holds a lock of its own).

    Reentrant, because the methods here call each other.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._issues: dict[str, Issue] = {}
        self._lock = threading.RLock()
        self._path = path
        if path is not None:
            self._issues = _load(path)

    @property
    def path(self) -> Path | None:
        """The file this store is mirrored to, or ``None`` for in memory only."""
        return self._path

    def add(self, issue: Issue) -> None:
        """Insert or replace an issue by its key, and save.

        **The one write point.** Every mutation below ends here, so the file
        is rewritten exactly once per change and nothing can change the store
        without changing the file.
        """
        with self._lock:
            self._issues[issue.key] = issue
            self._save()

    def _save(self) -> None:
        """Rewrite the state file atomically, if there is one.

        The whole snapshot goes to a sibling temporary file, is fsynced, and
        is renamed over the old one, so a reader — the next boot — sees either
        the previous state or this one and never a torn file. A save that
        fails is logged rather than raised: the store in memory is still
        right, and the file is what a *restart* will read, so failing the
        completion or the webhook that caused the write would trade a stale
        snapshot for a stuck agent.
        """
        if self._path is None:
            return
        snapshot = {
            "version": STATE_VERSION,
            "issues": [_encode(issue) for issue in self._issues.values()],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}."
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(snapshot, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._path)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as exc:
            logger.error("could not save the issue store to %s: %s", self._path, exc)

    def get(self, key: str) -> Issue | None:
        """Return the issue with ``key``, or ``None`` if absent."""
        with self._lock:
            return self._issues.get(key)

    def queue(
        self,
        key: str,
        repo: str,
        provider: Provider = DEFAULT_PROVIDER,
        model: str | None = None,
    ) -> Issue:
        """Mark issue ``key`` Queued against ``repo``, at the back of the queue.

        Re-inserting the key makes dict insertion order the queue (FIFO)
        order, so :meth:`next_queued` needs no separate queue structure.
        Unknown keys are upserted, as in :meth:`complete`.

        Only the repo, the harness and the model are known here. The agent's
        own directory is the workspace dispatch builds from them, so it is
        set there.
        """
        with self._lock:
            existing = self._issues.pop(key, None) or Issue(key=key, title="")
            issue = replace(
                existing,
                status=IssueStatus.QUEUED,
                repo=repo,
                directory="",
                provider=provider,
                model=model,
            )
            self.add(issue)
            return issue

    def next_queued(self) -> Issue | None:
        """Return the oldest Queued issue, or ``None`` if the queue is empty."""
        with self._lock:
            return next(
                (i for i in self._issues.values() if i.status is IssueStatus.QUEUED),
                None,
            )

    def complete(self, key: str) -> Issue:
        """Mark the issue ``key`` as Done, upserting a minimal issue if unknown.

        There is no dispatch/claim path yet (the store starts empty), so an
        unknown key is created rather than rejected.
        """
        with self._lock:
            existing = self._issues.get(key)
            issue = (
                replace(existing, status=IssueStatus.DONE)
                if existing is not None
                else Issue(key=key, title="", status=IssueStatus.DONE)
            )
            self.add(issue)
            return issue

    def block(self, key: str, blocker: str) -> Issue:
        """Mark the issue ``key`` as Blocked with ``blocker``, upserting if unknown.

        Mirrors :meth:`complete`: an unknown key is created rather than
        rejected, since there is no dispatch/claim path yet.
        """
        with self._lock:
            existing = self._issues.get(key)
            issue = (
                replace(existing, status=IssueStatus.BLOCKED, blocker=blocker)
                if existing is not None
                else Issue(
                    key=key, title="", status=IssueStatus.BLOCKED, blocker=blocker
                )
            )
            self.add(issue)
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
        with self._lock:
            existing = self._issues.get(key)
            if existing is None or existing.status is not IssueStatus.BLOCKED:
                return None
            issue = replace(existing, status=IssueStatus.IN_PROGRESS, blocker="")
            self.add(issue)
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
        with self._lock:
            existing = self._issues.get(key)
            if existing is None or existing.status not in IN_FLIGHT:
                return None
            issue = replace(existing, status=IssueStatus.ORPHANED, agent=None)
            self.add(issue)
            return issue

    def in_flight(self) -> list[Issue]:
        """Every issue with a live agent working it, busy or parked.

        The issues a Linear delivery can reach: activity only matters on
        issues foregent has an agent for. Library code, with the catch-up
        read it names the issues for
        (:func:`~foregent.linear.poll_comments`); nothing calls either today.
        """
        with self._lock:
            return [i for i in self.list_issues() if i.status in IN_FLIGHT]

    def list_issues(self) -> list[Issue]:
        """Return all issues, sorted by key for stable output."""
        with self._lock:
            return sorted(self._issues.values(), key=lambda issue: issue.key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._issues)

    def __iter__(self) -> Iterator[Issue]:
        with self._lock:
            return iter(self.list_issues())


def _encode(issue: Issue) -> dict[str, Any]:
    """The JSON shape of one issue: every field, enums by value."""
    return {
        "key": issue.key,
        "title": issue.title,
        "status": issue.status.value,
        "repo": issue.repo,
        "directory": issue.directory,
        "provider": issue.provider.value,
        "model": issue.model,
        "blocker": issue.blocker,
        "agent": (
            {
                "label": issue.agent.label,
                "conversation_id": issue.agent.conversation_id,
            }
            if issue.agent is not None
            else None
        ),
    }


def _decode(record: dict[str, Any]) -> Issue:
    """The inverse of :func:`_encode`. Strict: a field it does not know how
    to read raises, and the caller starts empty rather than guessing."""
    agent = record["agent"]
    return Issue(
        key=record["key"],
        title=record["title"],
        status=IssueStatus(record["status"]),
        repo=record["repo"],
        directory=record["directory"],
        provider=Provider(record["provider"]),
        model=record["model"],
        blocker=record["blocker"],
        agent=(
            AgentRef(agent["label"], agent["conversation_id"])
            if agent is not None
            else None
        ),
    )


def _load(path: Path) -> dict[str, Issue]:
    """The issues in ``path``, in file order, or none.

    **Anything short of a readable file of the current version is an empty
    store**, said in the log. A missing file is the ordinary first boot; a
    file that does not parse, or names a version this code does not write, is
    treated the same way rather than half-read, because a store built from a
    guess about a record is worse than one built from the live agents alone —
    which is exactly what the reconciliation that follows falls back to.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        logger.info("no state file at %s; starting empty", path)
        return {}
    except OSError as exc:
        logger.warning("could not read the state file %s: %s; starting empty", path, exc)
        return {}
    try:
        data = json.loads(text)
        version = data["version"]
        if version != STATE_VERSION:
            logger.warning(
                "state file %s is version %r, not %d; starting empty",
                path,
                version,
                STATE_VERSION,
            )
            return {}
        issues = [_decode(record) for record in data["issues"]]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.warning("state file %s is unreadable: %r; starting empty", path, exc)
        return {}
    return {issue.key: issue for issue in issues}
