"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client (``docs/PLAN.md`` §2, Bridge core).
Queued issues are dispatched to agents as capacity allows, through the
:class:`~foregent.agents.AgentManager` seam (§5.13) rather than to any one
harness, and a periodic tick asks Linear what changed on the issues it is
tracking, so an agent sees activity on its own issue whether it is working
or parked (§5.1).
Events reach an agent through a queue drained by a daemon thread (§5.1), so
whoever ingested one is never held behind an agent that is mid-turn.
``/webhooks/linear`` receives what Linear pushes and feeds that same queue
(§8, Q8), alongside the tick.
Also mounts the foregent MCP server (``complete_task``,
``report_blocked``) as streamable HTTP at ``/mcp``, so an agent's lifecycle
tools mutate this same in-process store directly instead of looping back over
HTTP.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from foregent import config, herdr, linear, mcp_servers, skills
from foregent.agents import (
    AgentError,
    AgentEventKind,
    AgentManager,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    issue_key_from_label,
    label_for,
)
from foregent.agents.herdr_claude import HerdrClaudeManager
from foregent.events import Event, delivery_message, wakes
from foregent.models import Issue, IssueStatus
from foregent.store import IN_FLIGHT, IssueStore

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
    watch_deliveries()
    poll_linear()
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

# Events waiting for the agent they are for, oldest first (docs/PLAN.md
# §5.1). One queue and one drainer, so two events for one agent reach it one
# at a time and in the order they were written, and whoever ingested them
# waited on neither. A long wait on one agent therefore holds up deliveries
# to another; capacity is one agent (§5.6), so there is no other agent to
# hold up, and a queue per agent is the change when capacity grows.
deliveries: queue.Queue[tuple[str, str]] = queue.Queue()

# How long to pause before offering a message to a busy agent again. `send`
# blocks on the harness for its own budget first, so this paces only the
# case where the harness is failing rather than the agent being busy.
DELIVERY_RETRY_SECONDS = 5.0


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


def watch_deliveries() -> None:
    """Hand queued messages to their agents, on a daemon thread (§5.1).

    In the shape of :func:`watch_agents`, and for the same reason: a send
    blocks for as long as its agent stays busy, which can be a whole turn,
    and no ingesting caller can be held that long — Linear retries any
    webhook delivery the bridge is slow to answer.

    One thread, so the queue's order is the delivery order. It survives a
    failed delivery: a drainer that died on one message would silently
    strand every message behind it.
    """

    def consume() -> None:
        while True:
            key, message = deliveries.get()
            try:
                send_queued(key, message)
            except Exception:
                logger.exception("delivering to %s failed", key)
            finally:
                deliveries.task_done()

    threading.Thread(target=consume, name="foregent-deliveries", daemon=True).start()


def send_queued(key: str, message: str) -> None:
    """Deliver one queued ``message`` to issue ``key``'s agent, then unblock it.

    The store is read here rather than trusted from the enqueue: an agent can
    die while its messages wait, and a message for an agent that is no longer
    there is dropped and logged rather than delivered to whatever holds the
    key next.

    **Sends first, unblocks second** (docs/PLAN.md §5.6): an agent that has
    not received the message is not awake yet, and a send that failed leaves
    the issue BLOCKED, with no rollback path to get wrong.
    """
    issue = store.get(key)
    if issue is None or issue.status not in IN_FLIGHT or issue.agent is None:
        status = issue.status if issue is not None else "not tracked"
        logger.warning(
            "dropped a message for %s: no agent to deliver to (%s)", key, status
        )
        return
    if not send_when_free(issue.agent, message):
        return
    if issue.status is IssueStatus.BLOCKED:
        store.unblock(key)


def send_when_free(ref: AgentRef, message: str) -> bool:
    """Send ``message`` once the agent is free; ``False`` if it died first.

    A busy agent is waited on for as long as its turn takes.
    :meth:`~foregent.agents.AgentManager.send` waits for the harness's own
    budget and fails when that runs out, and that failure means the agent is
    still working, not that the message is lost — so it is offered again. The
    one thing that ends the wait is the agent being gone, because then the
    message can never land.

    A harness that cannot be reached is not an agent that died, so that is
    retried too; the pause between attempts is what keeps a broken socket
    from spinning.

    A message can therefore reach an agent twice: a stalled prompt is
    reported as a failure and never landed, but a socket that dies just after
    one landed reports the same thing. That trade is deliberate — an agent
    told twice re-reads its issue and carries on, where an event dropped on a
    blinking socket is gone.
    """
    while True:
        try:
            manager.send(ref, message)
            return True
        except AgentError as exc:
            if agent_gone(ref):
                logger.warning(
                    "dropped a message for %s: agent is gone (%s)", ref.label, exc
                )
                return False
            logger.debug(
                "%s is not free yet, still trying to deliver: %s", ref.label, exc
            )
        time.sleep(DELIVERY_RETRY_SECONDS)


def agent_gone(ref: AgentRef) -> bool:
    """Whether the harness says the agent no longer exists.

    A harness that answers nothing reads as *not* gone: dropping an event
    because a socket blinked would lose it for good, and waiting costs only
    another attempt.
    """
    try:
        return manager.status(ref) is AgentStatus.GONE
    except AgentError as exc:
        logger.warning("cannot tell whether %s is still alive: %s", ref.label, exc)
        return False


# Foregent's own Linear account id, once something has had to ask for it.
# Empty until then; :func:`own_viewer` is the only thing that reads or writes it.
_viewer = ""


def own_viewer() -> str:
    """foregent's own Linear account id, asked for once and remembered.

    Every delivery is checked against it (:func:`~foregent.events.wakes`), so
    a bridge that re-asked would spend a Linear call per webhook on an answer
    that never changes. Raises :class:`~foregent.linear.LinearError` while it
    is unknown, because a delivery matched without it wakes agents with their
    own writes, and a wake that causes a write is a loop.

    Blocking: it is an HTTP call, so an async caller runs it in a threadpool.
    The tick carries its own viewer in its loop, where the cursor already
    lives.
    """
    global _viewer
    if not _viewer:
        _viewer = linear.viewer_id()
    return _viewer


def deliver(event: Event, viewer: str) -> None:
    """Hand ``event`` to whichever agent it was for, if any (docs/PLAN.md §5.1).

    Goes through :func:`deliver_issue` rather than the queue directly, so that
    the live-agent guard and the 409 for an issue with nobody behind it stay
    decided in one place. An event with nowhere to go is the normal case, not
    an error: the tick polls every in-flight issue, and a person comments on
    issues foregent is not tracking at all.

    The status is read once here only to word the prompt: a parked agent is
    being woken and a working one is not, and only the caller of a plain
    ``message`` endpoint can know which.
    """
    key = wakes(event, viewer)
    if not key:
        return
    issue = store.get(key)
    parked = issue is not None and issue.status is IssueStatus.BLOCKED
    try:
        deliver_issue(key, delivery_message(event, parked=parked))
    except HTTPException as exc:
        logger.debug("event on %s reached nobody: %s", key, exc.detail)
        return
    logger.info("queued for %s on activity by %s", key, event.author or "someone")


def poll_tick(cursor: str, viewer: str) -> tuple[str, str]:
    """Ask Linear what changed since ``cursor``, and deliver it. One pass.

    Returns the cursor and viewer to run the next pass with; both are held by
    the caller's loop rather than in module state, which is what lets a test
    drive a tick without a thread or a clock.

    The cursor only ever advances past comments this pass has served
    (:func:`~foregent.linear.poll_comments`), so a failure anywhere here
    leaves the window intact and the next pass re-reads it. **The viewer is
    resolved before anything is polled and the pass is abandoned without it**:
    a poll that cannot recognize foregent's own writes wakes agents with them,
    and a wake that causes a write is a loop.
    """
    try:
        viewer = viewer or linear.viewer_id()
        events, cursor = linear.poll_comments(
            [issue.key for issue in store.in_flight()], cursor, viewer
        )
    except linear.LinearError as exc:
        # Lateness is polling's failure mode and it is self-healing: the
        # cursor did not move, so the next tick asks for the same window.
        logger.warning("event poll failed, retrying next tick: %s", exc)
        return cursor, viewer
    for event in events:
        deliver(event, viewer)
    return cursor, viewer


def poll_linear() -> None:
    """Run :func:`poll_tick` forever, on its own thread (docs/PLAN.md §5.1).

    A daemon thread, like :func:`watch_agents`: it holds nothing the process
    would miss at exit, and its state is a cursor it can rebuild from the
    clock on the next boot.

    The first cursor is the clock — the only time it legitimately is one.
    Foregent has no durable record of what it has already delivered (§5.11),
    so a restart starts watching from now rather than replaying a backlog of
    comments its agents have most likely already acted on.
    """
    interval = config.poll_interval()
    logger.info("polling Linear for events every %gs", interval)

    def tick() -> None:
        cursor, viewer = datetime.now(UTC).isoformat(), ""
        while True:
            time.sleep(interval)
            cursor, viewer = poll_tick(cursor, viewer)

    threading.Thread(target=tick, name="foregent-linear-poll", daemon=True).start()


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


@app.post("/issues/{key}/deliver")
def deliver_issue(
    key: str, message: Annotated[str, Body(embed=True)]
) -> dict[str, str]:
    """Queue ``message`` for issue ``key``'s agent, and return the record.

    Every agent foregent has running is reachable, not only a parked one: a
    worker should see activity on its own issue as soon as it happens
    (docs/PLAN.md §5.1). The send itself waits for whatever the agent is
    doing to finish, so it happens on the drainer thread
    (:func:`watch_deliveries`) and this route only enqueues. What the caller
    is told is therefore that the message is *accepted*, not that it has been
    read: the issue comes back as it stands, so a parked one still reads
    BLOCKED until :func:`send_queued` has sent and unblocked it.

    409 for an issue with no agent to prompt, checked here rather than on the
    drainer so an event with nowhere to go is answered instead of queued.
    Both halves of the guard are needed and neither implies the other: a Done
    issue keeps the agent ref of the agent foregent has since stopped, and
    ``block()`` upserts an unknown key, so an issue can carry a blocker with
    nothing behind it.

    Capacity does not change and nothing is dispatched, whatever the status:
    the agent has been holding its slot the whole time.
    """
    issue = store.get(key)
    if issue is None or issue.status not in IN_FLIGHT or issue.agent is None:
        status = issue.status if issue is not None else "not tracked"
        raise HTTPException(
            status_code=409, detail=f"{key} has no agent to deliver to ({status})"
        )
    deliveries.put((key, message))
    return _record(issue)


@app.post("/webhooks/linear")
async def linear_webhook(request: Request) -> dict[str, str]:
    """Deliver what Linear pushes to the agent it is for (docs/PLAN.md §5.1).

    Authenticate, map the payload to an :class:`~foregent.events.Event`, and
    hand it to :func:`deliver` — the same matching, queue and drainer the tick
    feeds, so push is a second source rather than a second delivery path.

    **A delivery foregent does nothing with is still a success.** Most of what
    Linear sends is about issues no agent here is working, and a 200 is the
    honest answer: nothing failed, and telling Linear otherwise buys three
    pointless retries of an event that would be dropped again. That covers a
    payload naming no issue, an issue nobody is working, and foregent's own
    writes coming back at it.

    The one delivery that is *not* accepted is one that arrives while
    foregent's own account id is unknown (:func:`own_viewer`): matching
    without it would wake an agent with its own comment. Linear's retry is
    worth more here than a wake foregent has to guess at, so it answers 503
    and asks to be sent it again.

    The tick delivers the same comments in parallel with this, so an agent can
    be told about one twice, in whichever order the two paths land it. That is
    accepted rather than de-duplicated: an agent told twice re-reads its issue
    and carries on, and JIM-134 removes the tick, which ends it.

    Reads the raw bytes rather than a parsed body, because that is what the
    signature covers (:func:`~foregent.linear.webhook_authentic`). 401 for a
    delivery that does not prove it came from Linear, absent signature
    included; 503 when this bridge holds no secret to check one against, which
    is an operator's misconfiguration and not the caller's fault; 400 for a
    signed body that is not JSON, which is not a delivery Linear makes.
    """
    body = await request.body()
    try:
        authentic = linear.webhook_authentic(
            body, request.headers.get(linear.SIGNATURE_HEADER, "")
        )
    except linear.LinearError as exc:
        logger.error("cannot authenticate Linear webhooks: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not authentic:
        raise HTTPException(status_code=401, detail="signature does not match")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"not a Linear delivery: {exc}"
        ) from exc
    event = linear.webhook_event(payload)
    if event is None:
        logger.debug("Linear webhook is about no issue foregent knows: %s", payload)
        return {"status": "ok"}
    try:
        viewer = await run_in_threadpool(own_viewer)
    except linear.LinearError as exc:
        logger.error("cannot tell foregent's own writes apart: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    deliver(event, viewer)
    return {"status": "ok"}


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
