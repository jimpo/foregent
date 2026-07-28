"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client (``docs/PLAN.md`` §2, Bridge core).
Queued issues are dispatched to agents as capacity allows, through the
:class:`~foregent.agents.AgentManager` seam (§5.13) rather than to any one
harness. Also mounts the foregent MCP server (``complete_task``,
``report_blocked``) as streamable HTTP at ``/mcp``, so an agent's lifecycle
tools mutate this same in-process store directly instead of looping back over
HTTP.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from foregent import config, herdr, linear, mcp_servers, skills
from foregent.agents import (
    AgentError,
    AgentEventKind,
    AgentManager,
    AgentRef,
    LaunchSpec,
    issue_key_from_label,
    label_for,
)
from foregent.agents.herdr_claude import HerdrClaudeManager
from foregent.models import Issue, IssueStatus
from foregent.store import IssueStore

logger = logging.getLogger(__name__)

# stateless_http: these tools are fire-and-forget, so session-id bookkeeping
# would be pure overhead.
mcp = FastMCP("foregent", stateless_http=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Where agents will run is now resolved, not fixed (docs/PLAN.md §5.10),
    # so say it once at startup: everything after this — dispatch, recovery,
    # the operator's `herdr --session` — depends on it being the intended one.
    logger.info("running agents in %s", manager.describe())
    await run_in_threadpool(check_herdr_protocol)
    await run_in_threadpool(check_agent_mcp)
    await run_in_threadpool(rebuild_store)
    watch_agents()
    # mounting the streamable-HTTP sub-app below does not run *its* lifespan,
    # so the session manager has to be driven from here instead. By the time
    # this runs (server startup), `mcp.streamable_http_app()` has already
    # been called at import time (see the `app.mount` call at the bottom of
    # this module), so `mcp.session_manager` exists.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="foregent", lifespan=lifespan)

# The single, process-wide issue store this server serves. Empty until
# rebuild_store() runs at startup (JIM-52); Linear-side rebuild (titles etc.)
# lands with the rest of the bridge.
store = IssueStore()

# The harness foregent runs agents on. One process-wide manager, swapped
# wholesale to change harness (docs/PLAN.md §5.13).
manager: AgentManager = HerdrClaudeManager(session=config.herdr_session())


def check_herdr_protocol() -> None:
    """Refuse to start if herdr speaks a different protocol (docs/PLAN.md §5.8).

    herdr is a hard dependency (§7): every later call assumes the protocol
    this client was built against, so a drift raises here and stops the
    bridge outright instead of surfacing as a mystery error mid-dispatch.
    Talks to herdr directly rather than through ``manager`` — the dispatch
    path is harness-agnostic (§5.13), but this check is inherently herdr-
    specific.
    """
    herdr.HerdrClient(session=config.herdr_session()).check_protocol()


def rebuild_store() -> None:
    """Reconstruct the issue<->agent map from live agents (JIM-52).

    The store is a volatile in-memory cache (docs/PLAN.md §5.11); on startup
    every dispatched agent is recovered by parsing the issue key out of its
    label. Best-effort: a harness hiccup logs and leaves the store empty
    rather than blocking startup.
    """
    try:
        agents = manager.list_agents()
    except AgentError as exc:
        logger.warning("rebuild_store: agent harness unreachable: %s", exc)
        return
    for record in agents:
        key = issue_key_from_label(record.ref.label)
        if key is None:
            continue
        # Reconstructed as IN_PROGRESS: enough to hold the capacity-1 slot
        # and prevent double-launch. A BLOCKED issue also holds a live
        # agent, but distinguishing that (and full orphan reconciliation)
        # is docs/PLAN.md §5.12, out of scope here.
        store.add(
            Issue(
                key=key,
                title="",
                status=IssueStatus.IN_PROGRESS,
                directory=record.cwd,
                agent=record.ref,
            )
        )


def watch_agents() -> None:
    """Consume harness events, orphaning issues whose agent dies (JIM-87).

    The bridge learns about agent death from a subscription rather than a
    probe (docs/PLAN.md §5.6). The consumer is a daemon thread because the
    manager's stream is blocking and endless; it needs no shutdown path,
    since it holds nothing the process cares about losing at exit.
    """

    def consume() -> None:
        for event in manager.events():
            if event.kind is not AgentEventKind.EXITED:
                continue
            key = issue_key_from_label(event.ref.label)
            if key is None:
                continue
            issue = store.orphan(key)
            if issue is not None:
                # Orphaned frees the capacity slot: a dead agent must not go
                # on holding one. Deciding what happens next — re-dispatch,
                # defer, escalate — is the scheduler's (docs/PLAN.md §5.12).
                logger.warning("agent for %s exited; issue orphaned", key)

    threading.Thread(target=consume, name="foregent-agent-events", daemon=True).start()


def _record(issue: Issue) -> dict[str, str]:
    return {
        "key": issue.key,
        "title": issue.title,
        "status": issue.status,
        "blocker": issue.blocker,
    }


def brief_for(key: str) -> str:
    """The opening message an agent is given for issue ``key``.

    Invoking the skill by name leaves the lifecycle in one place — the skill —
    instead of half-restating it here, where the two would drift.
    """
    return f"/foregent-worker {key}"


def agent_mcp_servers() -> dict[str, dict]:
    """The MCP servers a dispatched agent is given.

    Foregent's own lifecycle tools, served from this process — without them
    an agent cannot report that it is blocked or done, so the bridge never
    learns the outcome of the work it dispatched. This one is per-run, which
    is why it is declared here rather than installed on the machine.

    Linear and GitHub are deliberately absent, and `strict_mcp` stays off:
    they are provisioned once per box by `foregent setup`
    (:mod:`foregent.mcp_servers`) and inherited, so one configuration serves
    agents and the operator's own sessions alike (JIM-93, docs/PLAN.md §5.2).
    """
    return {"foregent": {"type": "http", "url": f"{config.api_url()}/mcp"}}


def check_agent_mcp() -> None:
    """Warn if the box cannot give its agents Linear and GitHub (JIM-93).

    Agents inherit these from the machine, so an unprovisioned box dispatches
    agents that cannot read the issue they were sent to work — expensively,
    and only discovered once one is already running. A warning rather than a
    refusal: the fix is `foregent setup`, and a bridge that will not start is
    a worse way to say so.
    """
    absent = sorted(set(mcp_servers.SERVERS) - mcp_servers.configured())
    if absent:
        logger.warning(
            "%s MCP not configured on this machine; run `foregent setup`",
            ", ".join(absent),
        )
    for variable in mcp_servers.missing_credentials():
        logger.warning("%s is not set; agents cannot authenticate with it", variable)


def ensure_skills() -> None:
    """Install any packaged skill this machine is missing, before a launch.

    `foregent setup` is the deliberate installer; this is the safety net that
    keeps a box where it was never run from briefing an agent to use a skill
    that is not there. It only fills gaps — updating is setup's job.

    **Must complete before `manager.launch`, never alongside it.** Claude Code
    watches skill directories live, but only ones that existed when the
    session started: on a fresh box with no `~/.claude/skills/`, a skill
    written after the agent starts is invisible to that agent for its whole
    life.

    Best-effort. A box foregent cannot write skills to still gets its agent,
    working the issue without foregent's lifecycle instructions, which beats
    not dispatching at all.

    Knowing where a Claude Code session looks for skills is a harness detail
    leaking through the `AgentManager` seam (docs/PLAN.md §5.13). Acceptable
    while there is one harness; a second one makes this a manager method.
    """
    try:
        installed = skills.ensure()
    except OSError as exc:
        logger.warning("could not install foregent's skills: %s", exc)
        return
    for name in installed:
        logger.info("installed the %s skill into %s", name, skills.skills_root())


def dispatch() -> None:
    """Launch an agent for the oldest Queued issue, capacity allowing.

    Capacity is hardcoded at one concurrently running agent, occupied by an
    IN_PROGRESS or a parked-alive BLOCKED issue (docs/PLAN.md §5.6). Before
    launch, the issue is claimed directly in Linear (assignee + In Progress
    state, docs/PLAN.md §5.11-5.12) — no agent runs without a durable
    ownership record. On a Linear or harness failure the issue stays Queued
    and the caller's request fails with 502. Foregent's skills are installed
    first (:func:`ensure_skills`), because the agent cannot pick up one that
    appears after it starts.

    Dispatch is not atomic, and the deterministic agent label is what makes
    that survivable. If the brief fails to send after the agent starts, a
    retry finds the existing agent by label and adopts it instead of running
    a second one for the same issue. If the claim succeeds but the launch
    fails, Linear is left In Progress while the store keeps the issue Queued;
    that self-heals on retry, because claiming is idempotent — the durable
    fix for both is the reconciliation of §5.12.
    """
    occupied = (IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED)
    if any(issue.status in occupied for issue in store):
        return
    issue = store.next_queued()
    if issue is None:
        return
    label = label_for(issue.key)
    ensure_skills()
    try:
        linear.claim_issue(issue.key)
        ref = _adopt(label) or manager.launch(
            LaunchSpec(
                label=label,
                cwd=issue.directory,
                mcp_servers=agent_mcp_servers(),
            )
        )
        manager.send(ref, brief_for(issue.key))
    except linear.LinearError as exc:
        raise HTTPException(status_code=502, detail=f"Linear claim: {exc}") from exc
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=f"agent harness: {exc}") from exc
    store.add(replace(issue, status=IssueStatus.IN_PROGRESS, agent=ref))


def _adopt(label: str) -> AgentRef | None:
    """An already-running agent for ``label``, if a previous attempt left one."""
    for record in manager.list_agents():
        if record.ref.label == label:
            logger.info("adopting the agent already running as %s", label)
            return record.ref
    return None


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


@app.post("/issues/{key}/wake")
def wake_issue(key: str, message: Annotated[str, Body(embed=True)]) -> dict[str, str]:
    """Deliver ``message`` to issue ``key``'s parked agent and unblock it.

    The counterpart to :func:`block_issue` (docs/PLAN.md §5.6): the event the
    agent parked on has arrived, its process never died, and prompting it is
    the whole of waking it up. Capacity does not change and nothing is
    dispatched — the agent held its slot for the duration of the block.

    409 if the issue is not parked. That covers two cases: it is not BLOCKED
    at all, and it is BLOCKED with no agent recorded — ``block()`` upserts an
    unknown key, so an issue can carry a blocker with nothing to prompt.

    **Sends first, unblocks second**, so a harness failure leaves the issue
    BLOCKED with no rollback path to get wrong and a retry is safe. It is
    also the truthful order: an agent that has not received the message is
    not awake yet, and ``send`` can sit waiting for the harness for a while
    before it lands.
    """
    issue = store.get(key)
    if issue is None or issue.status is not IssueStatus.BLOCKED:
        status = issue.status if issue is not None else "not tracked"
        raise HTTPException(status_code=409, detail=f"{key} is {status}, not blocked")
    if issue.agent is None:
        raise HTTPException(status_code=409, detail=f"{key} has no agent to wake")
    try:
        manager.send(issue.agent, message)
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=f"agent harness: {exc}") from exc
    return _record(store.unblock(key) or issue)


@mcp.tool()
async def complete_task(issue_key: str) -> str:
    """Record ``issue_key`` as Done and shut down its agent."""
    # complete_issue's dispatch() call can block on the harness for a minute
    # or more; FastMCP runs sync tools inline on the event loop (no
    # auto-offload like Starlette gives sync FastAPI routes), so this must be
    # threadpooled to avoid stalling the whole server.
    try:
        await run_in_threadpool(complete_issue, issue_key)
        result = f"Marked {issue_key} complete."
    except HTTPException as exc:
        # store.complete already succeeded; only the follow-on dispatch
        # failed, and retrying complete is safe (server.py's /complete route
        # docstring) — so report success rather than raising a tool error.
        result = f"Marked {issue_key} complete; next dispatch failed: {exc.detail}"
    # Tear down the agent that called this. Best-effort: the issue is already
    # Done, so a failed teardown must not fail the tool.
    issue = store.get(issue_key)
    if issue is not None and issue.agent is not None:
        try:
            await run_in_threadpool(manager.stop, issue.agent)
        except AgentError as exc:
            return f"{result} Agent teardown failed: {exc}"
    return result


@mcp.tool()
async def report_blocked(issue_key: str, blocker: str) -> str:
    """Record ``blocker`` on ``issue_key`` in the foregent issue store.

    The agent stays parked alive in its workspace: this only records state,
    nothing is terminated, and there is no wake mechanism here (the bridge
    prompts the agent with the resolving event later; see docs/PLAN.md §5.6).
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
