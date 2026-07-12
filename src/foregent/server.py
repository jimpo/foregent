"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client (``docs/PLAN.md`` §2, Bridge core).
Queued issues are dispatched to CAO ``task_supervisor`` agents as capacity
allows. Also mounts the foregent MCP server (``complete_task``,
``report_blocked``) as streamable HTTP at ``/mcp``, so the ``task_supervisor``
agent's lifecycle tools mutate this same in-process store directly instead of
looping back over HTTP.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from foregent import cao, linear
from foregent.models import Issue, IssueStatus
from foregent.store import IssueStore

# stateless_http: these tools are fire-and-forget, so session-id bookkeeping
# would be pure overhead.
mcp = FastMCP("foregent", stateless_http=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # mounting the streamable-HTTP sub-app below does not run *its* lifespan,
    # so the session manager has to be driven from here instead. By the time
    # this runs (server startup), `mcp.streamable_http_app()` has already
    # been called at import time (see the `app.mount` call at the bottom of
    # this module), so `mcp.session_manager` exists.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="foregent", lifespan=lifespan)

# The single, process-wide issue store this server serves. Empty on startup;
# rebuilt from CAO + Linear once the bridge lands (JIM-52).
store = IssueStore()


def _record(issue: Issue) -> dict[str, str]:
    return {
        "key": issue.key,
        "title": issue.title,
        "status": issue.status,
        "blocker": issue.blocker,
    }


def dispatch() -> None:
    """Launch a task_supervisor for the oldest Queued issue, capacity allowing.

    Capacity is hardcoded at one concurrently running agent, occupied by an
    IN_PROGRESS or a parked-alive BLOCKED issue (docs/PLAN.md §5.6). Before
    launch, the issue is claimed directly in Linear (assignee + In Progress
    state, docs/PLAN.md §5.11-5.12) — the task_supervisor is not launched
    without a durable ownership record. On a Linear or CAO failure the issue
    stays Queued and the caller's request fails with 502.
    """
    occupied = (IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED)
    if any(issue.status in occupied for issue in store):
        return
    issue = store.next_queued()
    if issue is None:
        return
    # ponytail: not atomic — if send_message fails after create_session, the
    # unassigned supervisor session leaks and a retry launches a second one.
    # Accepted for the skeleton; session naming/adoption (JIM-52) is the fix.
    # Similarly, if claim_issue succeeds but create_session then fails, the
    # issue is left In Progress + assigned in Linear while the store keeps it
    # Queued; this self-heals on retry since claim_issue is idempotent (same
    # assignee+state, so re-claiming still succeeds) — the §5.12
    # orphan/reconciliation case.
    try:
        linear.claim_issue(issue.key)
        terminal = cao.create_session("task_supervisor", issue.directory)
        cao.send_message(
            terminal["id"],
            f"You are assigned Linear issue {issue.key}. "
            "Read it via the Linear MCP and drive it to completion.",
        )
    except linear.LinearError as exc:
        raise HTTPException(status_code=502, detail=f"Linear claim: {exc}") from exc
    except OSError as exc:  # URLError and friends
        raise HTTPException(status_code=502, detail=f"cao-server: {exc}") from exc
    store.add(
        replace(
            issue, status=IssueStatus.IN_PROGRESS, session=terminal["session_name"]
        )
    )


@app.get("/issues")
def list_issues() -> list[dict[str, str]]:
    """Return the tracked issues as ``{key, title, status, blocker}`` records."""
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


@app.post("/issues/{key}/block")
def block_issue(key: str, blocker: Annotated[str, Body(embed=True)]) -> dict[str, str]:
    """Mark issue ``key`` Blocked with ``blocker`` and return the record.

    Does not dispatch: a blocked agent parks alive in its workspace and keeps
    holding its capacity slot (docs/PLAN.md §5.6), so blocking must not free
    capacity or launch another agent.
    """
    issue = store.block(key, blocker)
    return _record(issue)


@mcp.tool()
async def complete_task(issue_key: str) -> str:
    """Record ``issue_key`` as Done and shut down its supervisor's CAO session."""
    # complete_issue's dispatch() call can block on a urllib call to
    # cao-server for up to ~60s; FastMCP runs sync tools inline on the event
    # loop (no auto-offload like Starlette gives sync FastAPI routes), so
    # this must be threadpooled to avoid stalling the whole server.
    try:
        await run_in_threadpool(complete_issue, issue_key)
        result = f"Marked {issue_key} complete."
    except HTTPException as exc:
        # store.complete already succeeded; only the follow-on dispatch
        # failed, and retrying complete is safe (server.py's /complete route
        # docstring) — so report success rather than raising a tool error.
        result = f"Marked {issue_key} complete; next dispatch failed: {exc.detail}"
    # Tear down this issue's own supervisor session (the caller). Best-effort:
    # the issue is already Done, so a failed teardown must not fail the tool.
    issue = store.get(issue_key)
    if issue is not None and issue.session:
        try:
            await run_in_threadpool(cao.delete_session, issue.session)
        except OSError as exc:
            return f"{result} Session teardown failed: {exc}"
    return result


@mcp.tool()
async def report_blocked(issue_key: str, blocker: str) -> str:
    """Record ``blocker`` on ``issue_key`` in the foregent issue store.

    The agent session stays parked alive in its workspace: this only records
    state, nothing is terminated, and there is no wake mechanism here (the
    bridge posts the resolving event later; see docs/PLAN.md §5.6).
    """
    await run_in_threadpool(block_issue, issue_key, blocker)
    return f"Recorded blocker {blocker!r} on {issue_key}."


# Mounted at "/" (not "/mcp") because streamable_http_app() already routes at
# its own streamable_http_path (default "/mcp") — mounting it at "/mcp" would
# yield "/mcp/mcp". Mounted last so the explicit REST routes above take
# precedence and this catch-all doesn't shadow them. Calling
# streamable_http_app() here (import time) is also what creates
# `mcp.session_manager` lazily, which `lifespan` above depends on.
app.mount("/", mcp.streamable_http_app())
