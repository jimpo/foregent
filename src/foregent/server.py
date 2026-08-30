"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client.
Queued issues are dispatched to agents as capacity allows, through the
:class:`~foregent.agents.AgentManager` seam rather than to any one
harness.
``/webhooks/linear`` receives what Linear pushes about the issues foregent is
tracking, so an agent sees activity on its own issue whether it is working or
parked. Events reach an agent through a queue drained by a daemon thread, so
whoever ingested one is never held behind an agent that is mid-turn.
``/webhooks/github`` is the same door for what GitHub pushes about the pull
requests those agents open; it authenticates a delivery and, for now, stops
there.
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
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool

from foregent import config, github, herdr, linear, mcp_servers, skills, workspaces
from foregent.agents import (
    AgentError,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    issue_key_from_label,
    label_for,
)
from foregent.agents.herdr_claude import HerdrClaudeManager
from foregent.events import delivery_message, wakes
from foregent.models import Issue, IssueStatus, Mode
from foregent.store import IN_FLIGHT, IssueStore

logger = logging.getLogger(__name__)

# stateless_http: these tools are fire-and-forget, so session-id bookkeeping
# would be pure overhead.
mcp = FastMCP("foregent", stateless_http=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Where agents will run is resolved, not fixed, so say it once at
    # startup: everything after this — dispatch, recovery, the operator's
    # `herdr --session` — depends on it being the intended one.
    logger.info("running agents in %s", manager.describe())
    await run_in_threadpool(check_herdr_protocol)
    await run_in_threadpool(check_agent_mcp)
    await run_in_threadpool(rebuild_store)
    watch_agents()
    watch_deliveries()
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
# wholesale to change harness.
manager: AgentManager = HerdrClaudeManager(session=config.herdr_session())

# Events waiting for the agent they are for, oldest first. One queue and one
# drainer, so two events for one agent reach it one at a time and in the order
# they were written, and whoever ingested them waited on neither. A long wait
# on one agent therefore holds up deliveries to another; capacity is one agent,
# so there is no other agent to hold up, and a queue per agent is the change
# when capacity grows.
deliveries: queue.Queue[tuple[str, str]] = queue.Queue()

# How long to pause before offering a refused message again. A prompt is
# submitted without waiting for the agent to be free, so a refusal is the
# harness being unreachable rather than the agent being busy.
DELIVERY_RETRY_SECONDS = 5.0


def check_herdr_protocol() -> None:
    """Refuse to start if herdr speaks a different protocol.

    herdr is a hard dependency: every later call assumes the protocol
    this client was built against, so a drift raises here and stops the
    bridge outright instead of surfacing as a mystery error mid-dispatch.
    Talks to herdr directly rather than through ``manager`` — the dispatch
    path is harness-agnostic, but this check is inherently herdr-
    specific.
    """
    herdr.HerdrClient(session=config.herdr_session()).check_protocol()


def rebuild_store() -> None:
    """Reconstruct the issue<->agent map from live agents (JIM-52).

    The store is a volatile in-memory cache; on startup
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
        # Reconstructed as IN_PROGRESS: enough to hold the capacity-1 slot and
        # prevent double-launch. A BLOCKED issue also holds a live agent, but
        # distinguishing that (and full orphan reconciliation) is out of scope
        # here.
        # `repo` is read back out of the workspace the agent is sitting in,
        # not remembered: a restart between dispatch and completion is the
        # ordinary case — the operator merges an agent's pull request and
        # picks the change up — and teardown needs the repo to forget the
        # workspace, so recovering an issue without it leaked a workspace
        # every time (JIM-150). Empty for an agent whose cwd is not a
        # workspace, which is the answer teardown wants there too.
        repo = workspaces.repo_for(Path(record.cwd)) if record.cwd else None
        store.add(
            Issue(
                key=key,
                title="",
                status=IssueStatus.IN_PROGRESS,
                repo=str(repo) if repo else "",
                directory=record.cwd,
                agent=record.ref,
            )
        )


def watch_agents() -> None:
    """Consume harness events, orphaning issues whose agent dies (JIM-87).

    The bridge learns about agent death from a subscription rather than a
    probe. The consumer is a daemon thread because the
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
                # Orphaned frees the capacity slot: a dead agent must not go on
                # holding one. Deciding what happens next — re-dispatch, defer,
                # escalate — is the scheduler's.
                logger.warning("agent for %s exited; issue orphaned", key)

    threading.Thread(target=consume, name="foregent-agent-events", daemon=True).start()


def watch_deliveries() -> None:
    """Hand queued messages to their agents, on a daemon thread.

    In the shape of :func:`watch_agents`, and for the same reason: a send
    talks to the harness and is retried until it lands, so it can take as
    long as the harness is unreachable, and no ingesting caller can be held
    that long — Linear retries any webhook delivery the bridge is slow to
    answer.

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

    **Sends first, unblocks second**: an agent that has
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
    if not send_now(issue.agent, message):
        return
    if issue.status is IssueStatus.BLOCKED:
        store.unblock(key)


def send_now(ref: AgentRef, message: str) -> bool:
    """Submit ``message`` to the agent; ``False`` if it died first.

    Ungated (``when_idle=False``): the message goes in whatever the agent is
    doing, because a worker is meant to see activity on its own issue as it
    happens. The harness queues a prompt behind the turn in progress, so
    delivering to a working agent costs it nothing and reaches it at the end
    of the turn it is in — where waiting for it to fall idle first reaches it
    only if it ever does, and an agent whose turn ends in ``complete_task``
    never does.

    A send that fails is offered again: the harness refusing a prompt says
    the agent is momentarily unreachable, not that the message is lost. The
    one thing that ends the retry is the agent being gone, because then the
    message can never land. A harness that cannot be reached at all is not an
    agent that died, so that is retried too; the pause between attempts is
    what keeps a broken socket from spinning.

    A message can therefore reach an agent twice: a stalled prompt is
    reported as a failure and never landed, but a socket that dies just after
    one landed reports the same thing. That trade is deliberate — an agent
    told twice re-reads its issue and carries on, where an event dropped on a
    blinking socket is gone.
    """
    while True:
        try:
            manager.send(ref, message, when_idle=False)
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
    """
    global _viewer
    if not _viewer:
        _viewer = linear.viewer_id()
    return _viewer


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
    agents and the operator's own sessions alike (JIM-93).
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
    leaking through the `AgentManager` seam. Acceptable
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
    IN_PROGRESS or a parked-alive BLOCKED issue. Before
    launch, the issue is claimed directly in Linear (assignee + In Progress
    state) — no agent runs without a durable
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
    fix for both is orphan reconciliation.
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
        running = _adopt(label)
        if running is not None:
            # A previous attempt got as far as launching, so it also built the
            # workspace; the agent's own cwd is where that ended up, and the
            # store's copy was never written.
            ref, cwd = running.ref, running.cwd
        else:
            # Before the launch, because the workspace is the agent's cwd.
            cwd = str(workspaces.create(Path(issue.repo), issue.key))
            ref = manager.launch(
                LaunchSpec(
                    label=label,
                    cwd=cwd,
                    mcp_servers=agent_mcp_servers(),
                )
            )
        manager.send(ref, brief_for(issue.key))
    except linear.LinearError as exc:
        raise HTTPException(status_code=502, detail=f"Linear claim: {exc}") from exc
    except workspaces.WorkspaceError as exc:
        raise HTTPException(status_code=502, detail=f"workspace: {exc}") from exc
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=f"agent harness: {exc}") from exc
    store.add(
        replace(issue, status=IssueStatus.IN_PROGRESS, directory=cwd, agent=ref)
    )


def _adopt(label: str) -> AgentRecord | None:
    """An already-running agent for ``label``, if a previous attempt left one.

    The whole record, not just the ref: a retry needs the agent's cwd as well,
    because the workspace a failed attempt built is not in the store.
    """
    for record in manager.list_agents():
        if record.ref.label == label:
            logger.info("adopting the agent already running as %s", label)
            return record
    return None


@app.get("/issues")
def list_issues() -> list[dict[str, str]]:
    """Return the tracked issues as ``{key, title, status, blocker}`` records."""
    return [_record(issue) for issue in store.list_issues()]


@app.post("/issues/{key}/queue")
def queue_issue(key: str, directory: Annotated[str, Body(embed=True)]) -> dict[str, str]:
    """Queue issue ``key`` against the repo at ``directory``, dispatching if free.

    ``directory`` is the project, not the agent's cwd: dispatch builds a
    per-issue workspace from it and runs the agent there
    (:mod:`foregent.workspaces`).
    """
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
    holding its capacity slot, so blocking must not free
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
   . The send itself waits for whatever the agent is
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


def _payload(body: bytes) -> dict:
    """The JSON object an authenticated delivery holds.

    Raises a 400 for anything else. Both providers are configured for JSON
    delivery and neither sends anything but an object, so a body that is not
    one is not a delivery either of them makes — saying so beats pretending
    it was handled.
    """
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"not a delivery: {exc}") from exc
    return payload


@app.post("/webhooks/linear")
async def linear_webhook(request: Request) -> dict[str, str]:
    """Deliver what Linear pushes to the agent it is for.

    Push is the whole of foregent's inbound path: authenticate, map the
    payload to an :class:`~foregent.events.Event`, match it to an issue, and
    queue it for that issue's agent. The enqueue goes through
    :func:`deliver_issue` rather than the queue directly, so the live-agent
    guard and the 409 for an issue with nobody behind it stay decided in one
    place. The issue's status is read here only to word the prompt: a parked
    agent is being woken and a working one is not.

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
    payload = _payload(body)
    event = linear.webhook_event(payload)
    if event is None:
        logger.debug("Linear webhook is about no issue foregent knows: %s", payload)
        return {"status": "ok"}
    try:
        viewer = await run_in_threadpool(own_viewer)
    except linear.LinearError as exc:
        logger.error("cannot tell foregent's own writes apart: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    key = wakes(event, viewer)
    if not key:
        return {"status": "ok"}
    issue = store.get(key)
    parked = issue is not None and issue.status is IssueStatus.BLOCKED
    try:
        deliver_issue(key, delivery_message(event, parked=parked))
    except HTTPException as exc:
        logger.debug("event on %s reached nobody: %s", key, exc.detail)
        return {"status": "ok"}
    logger.info("queued for %s on activity by %s", key, event.author or "someone")
    return {"status": "ok"}


@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict[str, str]:
    """Accept what GitHub pushes about the pull requests foregent's agents open.

    Receipt and authentication. Mapping a delivery to an
    :class:`~foregent.events.Event`, resolving the pull request back to the
    Linear issue it is linked to, and prompting that issue's agent land in
    JIM-141; until they do, an authenticated delivery is logged and dropped.

    **A delivery foregent does nothing with is still a success**, as on the
    Linear side: an organization webhook carries every repository and every
    pull request, most of them none of foregent's business, and a failure
    code buys retries of an event that would be dropped again. The `ping`
    GitHub sends when the webhook is created is accepted on the same terms,
    which is what tells an operator the endpoint is wired up.

    Reads the raw bytes rather than a parsed body, because that is what the
    signature covers (:func:`~foregent.github.webhook_authentic`). 401 for a
    delivery that does not prove it came from GitHub, absent signature
    included; 503 when this bridge holds no secret to check one against,
    which is an operator's misconfiguration and not the caller's fault; 400
    for a signed body that is not a JSON object, which is what a webhook set
    to form-encoded delivery sends.
    """
    body = await request.body()
    try:
        authentic = github.webhook_authentic(
            body, request.headers.get(github.SIGNATURE_HEADER, "")
        )
    except github.GitHubError as exc:
        logger.error("cannot authenticate GitHub webhooks: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not authentic:
        raise HTTPException(status_code=401, detail="signature does not match")
    payload = _payload(body)
    # The body names the repository and the pull request; only the header says
    # what happened to them.
    kind = request.headers.get(github.EVENT_HEADER) or "nameless"
    logger.debug("GitHub delivered a %s event: %s", kind, payload)
    return {"status": "ok"}


@mcp.tool()
async def complete_task(issue_key: str) -> str:
    """Record ``issue_key`` as Done, land its work, and shut down its agent.

    The order is load-bearing, and the reason is the next issue. Completing
    dispatches whatever is queued behind this one, and that dispatch builds
    its workspace on ``main`` — so in bootstrap mode ``main`` has to be moved
    onto this issue's work *first*, or the next agent starts from a trunk this
    issue never reached and silently drops it from its base. Advancing also
    has to come before the teardown below, because the revision it names lives
    in the workspace it would remove.
    """
    issue = store.get(issue_key)
    landed = await land(issue_key, issue)
    if landed is not None:
        return landed
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
    if issue is not None and issue.agent is not None:
        try:
            await run_in_threadpool(manager.stop, issue.agent)
        except AgentError as exc:
            return f"{result} Agent teardown failed: {exc}"
    # Then the workspace, and only after the agent is gone — removing a live
    # agent's own cwd out from under it is worse than leaking a directory.
    # The work is already in git by now, so a failure here costs a directory
    # and not the issue's commits; it is still logged rather than passed over,
    # because nobody owns the leftovers.
    if issue is not None and issue.repo and issue.directory:
        try:
            await run_in_threadpool(
                workspaces.destroy,
                Path(issue.repo),
                issue_key,
                Path(issue.directory),
            )
        except workspaces.WorkspaceError as exc:
            logger.error("could not remove the %s workspace: %s", issue_key, exc)
            return f"{result} The workspace was left behind ({exc})."
    return result


async def land(issue_key: str, issue: Issue | None) -> str | None:
    """Move ``main`` onto a bootstrap issue's work; a refusal, or ``None``.

    Bootstrap mode has no pull request to carry the work out of the
    workspace, so this is what lands it: the bridge moves the bookmark at the
    colocated repo root, where jj exports it to git
    (:func:`foregent.workspaces.advance`). Pull Request mode has already
    pushed its own branch, so there is nothing to do and ``main`` is the
    reviewer's to move.

    The mode is read off the repo again rather than remembered from dispatch.
    It is a pure function of the remotes, and ``issue.repo`` survives a
    restart where a stored mode would not (:func:`rebuild_store`).

    **A refusal stops the completion short**, and is the one thing in this
    path that does. jj declines to move ``main`` onto work that is not
    descended from it, which means an agent that never rebased: its commits
    exist only in the workspace, and going on would tear that workspace down
    and take them with it. Returning the message leaves the issue in flight
    and the workspace on disk for the operator, which is the recoverable half
    of a bad outcome.

    Only an in-flight issue is landed, which is what keeps completing twice
    safe. The second call has no workspace left to name a revision in, and jj
    would refuse the move for a reason that says nothing about the work.
    """
    if issue is None or issue.status not in IN_FLIGHT or not issue.repo:
        return None
    repo = Path(issue.repo)
    if workspaces.mode_for(repo) is not Mode.BOOTSTRAP:
        return None
    try:
        await run_in_threadpool(workspaces.advance, repo, issue_key)
    except workspaces.WorkspaceError as exc:
        logger.error("could not advance %s for %s: %s", workspaces.TRUNK, issue_key, exc)
        return (
            f"{issue_key} was not completed: {workspaces.TRUNK} could not be "
            f"moved onto its work ({exc}). The workspace is still there, and "
            f"the commits are only in it. Rebase onto {workspaces.TRUNK} and "
            f"call complete_task again."
        )
    return None


@mcp.tool()
async def report_blocked(issue_key: str, blocker: str) -> str:
    """Record ``blocker`` on ``issue_key`` in the foregent issue store.

    The agent stays parked alive in its workspace: this only records state,
    nothing is terminated, and there is no wake mechanism here (the bridge
    prompts the agent with the resolving event later).
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
