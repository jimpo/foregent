"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client (``docs/PLAN.md`` §2, Bridge core).
Queued issues are dispatched to CAO ``task_supervisor`` agents as capacity
allows.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException

from foregent import cao
from foregent.models import Issue, IssueStatus
from foregent.store import IssueStore

app = FastAPI(title="foregent")

# The single, process-wide issue store this server serves. Empty on startup;
# rebuilt from CAO + Linear once the bridge lands (JIM-52).
store = IssueStore()


def _record(issue: Issue) -> dict[str, str]:
    return {"key": issue.key, "title": issue.title, "status": issue.status}


def dispatch() -> None:
    """Launch a task_supervisor for the oldest Queued issue, capacity allowing.

    Capacity is hardcoded at one concurrently running agent. On a CAO failure
    the issue stays Queued and the caller's request fails with 502.
    """
    if any(issue.status is IssueStatus.IN_PROGRESS for issue in store):
        return
    issue = store.next_queued()
    if issue is None:
        return
    # ponytail: not atomic — if send_message fails after create_session, the
    # unassigned supervisor session leaks and a retry launches a second one.
    # Accepted for the skeleton; session naming/adoption (JIM-52) is the fix.
    try:
        terminal = cao.create_session("task_supervisor", issue.directory)
        cao.send_message(
            terminal["id"],
            f"You are assigned Linear issue {issue.key}. "
            "Read it via the Linear MCP and drive it to completion.",
        )
    except OSError as exc:  # URLError and friends
        raise HTTPException(status_code=502, detail=f"cao-server: {exc}") from exc
    store.add(replace(issue, status=IssueStatus.IN_PROGRESS))


@app.get("/issues")
def list_issues() -> list[dict[str, str]]:
    """Return the tracked issues as ``{key, title, status}`` records."""
    return [_record(issue) for issue in store.list_issues()]


@app.post("/issues/{key}/queue")
def queue_issue(key: str, directory: Annotated[str, Body(embed=True)]) -> dict[str, str]:
    """Queue issue ``key`` to run in ``directory``, dispatching if capacity allows."""
    existing = store.get(key)
    if existing is not None and existing.status in (
        IssueStatus.QUEUED,
        IssueStatus.IN_PROGRESS,
    ):
        raise HTTPException(
            status_code=409, detail=f"{key} is already {existing.status}"
        )
    issue = store.queue(key, directory)
    dispatch()
    return _record(store.get(key) or issue)


@app.post("/issues/{key}/complete")
def complete_issue(key: str) -> dict[str, str]:
    """Mark issue ``key`` Done, dispatch the next queued issue, and return the record."""
    issue = store.complete(key)
    # The completion above sticks even if dispatch 502s: the caller sees the
    # error, but the issue is Done and the next one stays Queued until a later
    # queue/complete triggers dispatch again. Retrying complete is safe.
    dispatch()
    return _record(issue)
