"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client.
Queued issues are dispatched to agents as capacity allows, through the
:class:`~foregent.agents.AgentManager` seam rather than to any one
harness.
``/webhooks/linear`` receives what Linear pushes about the issues foregent is
tracking, so an agent sees activity on its own issue whether it is working or
parked. Events reach an agent through its own queue, drained by a daemon
thread, so whoever ingested one is never held behind an agent that is mid-turn
and no agent is held behind another.
``/webhooks/github`` is the same door for what GitHub pushes about the pull
requests those agents open, matched to an agent by the issue the pull request's
branch names. Both routes account for every delivery at debug level: one line
as it arrives, and one where it is delivered, filtered out, or not understood.
``/health`` reports when Linear last delivered, because push is
the only thing that wakes an agent and a hook that has stopped looks like quiet.
Also mounts the foregent MCP server (``complete_task``, ``report_blocked``,
``queue_sub_issues``) as streamable HTTP at ``/mcp``, so an agent's lifecycle
tools mutate this same in-process store directly instead of looping back over
HTTP.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Request
from mcp.server.mcpserver import MCPServer
from starlette.concurrency import run_in_threadpool

from foregent import config, github, herdr, linear, mcp_servers, skills, workspaces
from foregent.agents import (
    DEFAULT_PROVIDER,
    AgentError,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    McpServer,
    Provider,
    issue_key_from_label,
    label_for,
)
from foregent.agents.harness import harness_for
from foregent.agents.herdr_manager import HerdrManager
from foregent.events import Event, EventKind, delivery_message, wakes
from foregent.models import Issue, IssueStatus, Mode
from foregent.store import IN_FLIGHT, IssueStore

logger = logging.getLogger(__name__)

mcp = MCPServer("foregent")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Where agents will run is resolved, not fixed, so say it once at
    # startup: everything after this — dispatch, recovery, the operator's
    # `herdr --session` — depends on it being the intended one.
    logger.info("running agents in %s", manager.describe())
    await run_in_threadpool(check_herdr_protocol)
    await run_in_threadpool(check_agent_mcp)
    await run_in_threadpool(open_store)
    await run_in_threadpool(rebuild_store)
    watch_agents()
    # Whatever came back Queued has been waiting since before the restart,
    # and nothing else dispatches until the next queue or completion.
    await run_in_threadpool(dispatch_at_boot)
    # mounting the streamable-HTTP sub-app below does not run *its* lifespan,
    # so the session manager has to be driven from here instead. By the time
    # this runs (server startup), `mcp.streamable_http_app()` has already
    # been called at import time (see the `app.mount` call at the bottom of
    # this module), so `mcp.session_manager` exists.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="foregent", lifespan=lifespan)

# The single, process-wide issue store this server serves. In memory only
# until open_store() swaps in the persisted one at startup, so importing this
# module reads nothing off the box; rebuild_store() then reconciles what the
# file held against the live agents (JIM-249).
store = IssueStore()

# The harness foregent runs agents on. One process-wide manager, swapped
# wholesale to change harness.
manager: AgentManager = HerdrManager(session=config.herdr_session())

# Events waiting for the agent they are for, one queue per issue key, oldest
# first. Each has a drainer of its own, so two events for one agent reach it
# in batches and in the order they were written, whoever ingested them
# waited on neither, and an agent that cannot be reached delays only its own
# messages. `None` is the sentinel that ends a drainer.
deliveries: dict[str, queue.Queue[tuple[str, float] | None]] = {}

# Guards the dict above, not the queues in it: a queue is created on the first
# delivery to an issue, and two deliveries arriving together must find the same
# one rather than start two drainers for one agent.
_deliveries_lock = threading.Lock()

# Held for the whole of one dispatch. Dispatch reads the store for a free slot
# and writes the launched agent back only after several harness calls, so two
# callers arriving together would both read a store neither has written yet and
# launch the same issue twice. Launches are therefore serial even when several
# slots are free, which also keeps concurrent `jj workspace add` off one repo.
# ponytail: one lock for the fleet; per-repo locks if launch latency matters.
_dispatching = threading.Lock()

# Run slots (JIM-248): In Progress, not the wider In Flight `admits` also
# checks. A parked agent keeps its live slot but gives this one back, so
# waking it needs one the same as a fresh launch does. `_run_slots` guards
# both sets below and is notified whenever a run slot might have freed.
_run_slots = threading.Condition()

# Issue keys a drainer is currently trying to wake, granted a run slot or
# still waiting for one. Non-empty, this is what makes `admits` refuse a
# fresh launch: a parked agent already holds the memory and disk a new agent
# would need built, finishing it is what frees them, and it is the older
# work, so it is served first ("wake before fork").
_waking: set[str] = set()

# The subset of `_waking` granted a run slot and mid-send: what `_active`
# adds to the store's own In Progress count, because sending happens before
# unblocking (`send_queued`), so the store does not show the slot as taken
# until the send lands.
_claimed: set[str] = set()

# How many recent Linear deliveries are remembered, newest last, to answer a
# repeat of one already acted on. Linear retries a delivery it believes
# failed, and a retried comment must not prompt a worker twice. The signature
# is the key: there is no per-delivery id in the payload, but Linear signs the
# exact bytes it sent, so a retry carries the signature already seen and two
# distinct deliveries — each with its own `webhookTimestamp` — never collide.
# ponytail: a linear scan of a short deque; a set beside it if this grows.
RECENT_DELIVERIES = 256
_recent: deque[str] = deque(maxlen=RECENT_DELIVERIES)

# When an authentic Linear delivery last arrived, as an ISO-8601 instant, or
# empty until one has. Push is the only thing that tells a worker its issue
# moved, so a webhook that has stopped delivering otherwise looks like a quiet
# day; this is what :func:`health` reports so it does not.
_last_delivery = ""

# How long to pause before offering a refused message again. A prompt is
# submitted without waiting for the agent to be free, so a refusal is the
# harness being unreachable rather than the agent being busy.
DELIVERY_RETRY_SECONDS = 5.0

# Wait for a quiet period after the latest queued notification (JIM-267).
DELIVERY_DEBOUNCE_SECONDS = 5.0


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


# What a live agent's harness status says about its issue at boot, for the
# statuses that say anything. An agent that is not mid-turn has finished one
# and is waiting, which in this system means it parked (JIM-168) — these are
# the two the manager itself already reads as an agent that is free.
#
# The rest are absent on purpose. UNKNOWN means the state could not be read,
# not that the agent stopped, and herdr's own BLOCKED is an agent waiting on
# input rather than on the world; claiming either had parked would be a guess
# in the direction that wakes agents that never asked to be woken.
_RECOVERED = {
    AgentStatus.IDLE: IssueStatus.BLOCKED,
    AgentStatus.DONE: IssueStatus.BLOCKED,
}

# The blocker a recovered issue carries. The words a worker chose are gone
# with the process that held them, and the blocker is a note rather than a key
# (docs/ARCHITECTURE.md §1.6) — nothing matches on it, so saying plainly that
# it is unknown costs the operator nothing a re-report would have bought.
RECOVERED_BLOCKER = "unknown — recovered at restart"


def open_store() -> None:
    """Replace the store with the one persisted at :func:`config.state_file`.

    Everything the file holds is foregent's own intent — queue order, agent
    bindings, blockers — and none of it is trusted about the world until
    :func:`rebuild_store` has checked it against the harness.
    """
    global store
    store = IssueStore(config.state_file())
    logger.info("issue store at %s: %d issues", store.path, len(store))


def rebuild_store() -> None:
    """Reconcile the loaded store against the live agents (JIM-52, JIM-249).

    Linear holds issue truth, the harness holds liveness, and the store holds
    what foregent meant to do; one ``agent.list`` is where the last two meet.
    Every stored in-flight issue is checked against it:

    - **Its agent is gone → Orphaned.** The slot is freed, as it would have
      been had the bridge been up to see the agent exit.
    - **Its agent is live → the stored record stands**, title, blocker text
      and conversation id included, with whether it is parked taken from the
      harness (``_RECOVERED``). A status that says nothing — the state could
      not be read, or the agent is waiting on input rather than on the world
      — leaves the stored status as it was: the record is evidence, and the
      guess it used to fall back to was only ever for having none.
    - **A live agent the store does not know is adopted**, as a restart with
      no file at all recovers it: the issue key out of its label, the repo
      out of its cwd, the harness out of its kind, and a placeholder for the
      blocker whose words died with the old process.

    Queued issues need no reconciling; they have no agent to check and come
    back in the order they were queued.

    Best-effort: a harness that cannot be reached logs and leaves the store
    as the file had it, rather than blocking startup or forgetting what was
    written. The drainers already cope with an agent that cannot be reached.
    """
    try:
        agents = manager.list_agents()
    except AgentError as exc:
        logger.warning("rebuild_store: agent harness unreachable: %s", exc)
        return
    live: dict[str, AgentRecord] = {}
    for record in agents:
        key = issue_key_from_label(record.ref.label)
        if key is not None:
            live[key] = record
    for issue in store.in_flight():
        record = live.pop(issue.key, None)
        if record is None:
            logger.warning("agent for %s is gone since the last run; issue orphaned", issue.key)
            store.orphan(issue.key)
            continue
        store.add(_reconciled(issue, record))
    for key, record in live.items():
        store.add(_adopted(key, record))


def _reconciled(issue: Issue, record: AgentRecord) -> Issue:
    """``issue`` as stored, with what the harness says about its agent.

    Only the status moves, and only where the harness's own says which way.
    A blocker survives where the issue stays parked; one the store never had
    — the bridge went down while the agent worked, and it parked since — is
    the same placeholder an adopted agent gets.
    """
    status = _RECOVERED.get(record.status)
    if record.status is AgentStatus.WORKING:
        status = IssueStatus.IN_PROGRESS
    if status is None or status is issue.status:
        return issue
    blocker = (issue.blocker or RECOVERED_BLOCKER) if status is IssueStatus.BLOCKED else ""
    return replace(issue, status=status, blocker=blocker)


def _adopted(key: str, record: AgentRecord) -> Issue:
    """An issue for a live agent the store did not know.

    Whether it was parked comes from the harness's own status rather than from
    the label, which does not record it. Getting it back matters beyond the
    operator's table: a push to ``main`` wakes the issues that are Blocked
    (:func:`wake_on_push`), and in Pull Request mode the steady state is a
    fleet of agents all waiting on review, so a restart that returned them all
    as working left that wake with nobody to find.
    """
    # Either status holds the capacity slot and prevents a double launch;
    # which one it is decides whether a push to `main` reaches the agent.
    status = _RECOVERED.get(record.status, IssueStatus.IN_PROGRESS)
    # `repo` is read back out of the workspace the agent is sitting in: a
    # secondary workspace names the repo it belongs to, and teardown needs it
    # to forget the workspace (JIM-150). Empty for an agent whose cwd is not a
    # workspace, which is the answer teardown wants there too.
    repo = workspaces.repo_for(Path(record.cwd)) if record.cwd else None
    return Issue(
        key=key,
        title="",
        status=status,
        repo=str(repo) if repo else "",
        directory=record.cwd,
        # Which harness the agent runs is the agent kind herdr detected. An
        # agent of a kind foregent does not know reads as the default, which
        # costs nothing: the provider decides a brief, a skill directory and
        # a workspace's trust, and this issue has been dispatched already.
        # What is left of its life — a prompt, a status, a stop — is the same
        # call whatever it runs.
        provider=record.provider or DEFAULT_PROVIDER,
        blocker=RECOVERED_BLOCKER if status is IssueStatus.BLOCKED else "",
        agent=record.ref,
    )


def dispatch_at_boot() -> None:
    """Dispatch what the store came back with, without failing the boot.

    A queued issue survives a restart now, and dispatch otherwise runs only on
    a queue or a completion, so a bridge that came up with a queue and no
    agents would sit until the operator queued something else. A failure here
    is what it would be at the CLI, a 502 with a reason, and is logged as
    such: the issue stays Queued and the next queue or completion retries.
    """
    try:
        dispatch()
    except HTTPException as exc:
        logger.error("dispatch at boot failed: %s", exc.detail)


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
            # Whether or not there was an issue to orphan, nothing can reach
            # this agent again, so its drainer has no more work to wait for.
            stop_deliveries(key)

    threading.Thread(target=consume, name="foregent-agent-events", daemon=True).start()


def deliveries_for(key: str) -> queue.Queue[tuple[str, float] | None]:
    """Issue ``key``'s delivery queue, with a drainer running behind it.

    Created on the first delivery to an issue rather than at dispatch, so an
    agent nobody has written to costs no thread.
    """
    with _deliveries_lock:
        pending = deliveries.get(key)
        if pending is None:
            pending = deliveries[key] = queue.Queue()
            threading.Thread(
                target=drain,
                args=(key, pending),
                name=f"foregent-deliveries-{key}",
                daemon=True,
            ).start()
        return pending


def stop_deliveries(key: str) -> None:
    """End issue ``key``'s drainer; its agent is finished or gone.

    The sentinel goes to the back of the queue rather than clearing it, so
    messages already waiting are handled (and, for an issue that is no longer
    in flight, logged as dropped by :func:`send_queued`) before the thread
    ends. Absence is not an error: an issue nobody delivered to has no queue.
    """
    with _deliveries_lock:
        pending = deliveries.pop(key, None)
    if pending is not None:
        pending.put(None)


def drain(key: str, pending: queue.Queue[tuple[str, float] | None]) -> None:
    """Send one ordered batch after five seconds without a new notification.

    Runs on a daemon thread, for the reason :func:`watch_agents` does: a send
    talks to the harness and is retried until it lands, so it can take as long
    as the harness is unreachable, and no ingesting caller can be held that
    long — Linear retries any webhook delivery the bridge is slow to answer.

    One thread per issue, so this queue's order is that agent's delivery order
    and no other agent waits on this one. It survives a failed delivery: a
    drainer that died on one message would silently strand every message
    behind it.
    """
    while True:
        item = pending.get()
        count = 1
        stopping = item is None
        try:
            if item is not None:
                message, added = item
                messages = [message]
                while DELIVERY_DEBOUNCE_SECONDS > 0:
                    # A backlog left by a slow send must not start a fresh
                    # quiet period when it is consumed.
                    timeout = max(
                        0.0, added + DELIVERY_DEBOUNCE_SECONDS - time.monotonic()
                    )
                    try:
                        item = pending.get(timeout=timeout)
                    except queue.Empty:
                        break
                    count += 1
                    if item is None:
                        stopping = True
                        break
                    message, added = item
                    messages.append(message)
                send_queued(key, "\n\n".join(messages))
        except Exception:
            logger.exception("delivering to %s failed", key)
        finally:
            for _ in range(count):
                pending.task_done()
        if stopping:
            return


def _active() -> int:
    """Run slots taken right now: In Progress in the store, plus a wake
    granted one and mid-send (:data:`_claimed`), JIM-248.

    Caller holds :data:`_run_slots`.
    """
    working = sum(1 for tracked in store if tracked.status is IssueStatus.IN_PROGRESS)
    return working + len(_claimed)


def _await_run_slot(key: str) -> bool:
    """Block the calling drainer until waking issue ``key`` can take a run slot.

    Entering :data:`_waking` is what makes :func:`admits` hold a fresh launch
    back while this call waits: a parked agent already holds the memory and
    disk a new one would need built, so finishing it is what frees the
    scarce resource, and it is the older work (JIM-248, "wake before fork").

    Returns ``False`` if ``key`` stopped being worth waking while this
    waited — its agent died, or something else already unblocked it — so the
    caller drops the message instead of sending to whatever holds the key
    next.
    """
    with _run_slots:
        _waking.add(key)
        try:
            while True:
                issue = store.get(key)
                if issue is None or issue.status is not IssueStatus.BLOCKED:
                    return False
                if _active() < config.max_active():
                    _claimed.add(key)
                    return True
                _run_slots.wait()
        finally:
            _waking.discard(key)


def _give_back_run_slot(key: str | None = None) -> None:
    """Notify anyone waiting on a run slot that one might have freed.

    ``key`` is a claim in :data:`_claimed` to give up along with it — a
    wake's own, or a fresh launch's own reservation for the span of its call
    (:func:`_dispatch_one`) — since neither is reflected in the store's own
    In Progress count until it lands; ``None`` for a slot freed by a plain
    block or completion, which claimed nothing there.
    """
    with _run_slots:
        if key is not None:
            _claimed.discard(key)
        _run_slots.notify_all()


def _release_run_slot(key: str | None = None) -> None:
    """Give back a run slot (:func:`_give_back_run_slot`), then dispatch
    what is queued.

    Notifying first is what gives a drainer already waiting in
    :func:`_await_run_slot` first claim on the slot ahead of a fresh
    dispatch (JIM-248, "wake before fork") — :func:`dispatch` reads the
    store fresh regardless, so calling it too early only costs a queued
    issue one more cycle of waiting.

    Not for :func:`_dispatch_one` to call on its own claim: it already runs
    inside :func:`dispatch`'s loop, and :data:`_dispatching` is not
    reentrant.
    """
    _give_back_run_slot(key)
    dispatch()


def send_queued(key: str, message: str) -> None:
    """Deliver one queued ``message`` to issue ``key``'s agent, then unblock it.

    The store is read here rather than trusted from the enqueue: an agent can
    die while its messages wait, and a message for an agent that is no longer
    there is dropped and logged rather than delivered to whatever holds the
    key next.

    **Waking a parked agent needs a run slot** (JIM-248): if none is free,
    this call blocks in :func:`_await_run_slot` until the issue's own wake is
    granted one, rather than sending. A working or reviewing agent needs
    nothing of the kind — it already holds its run slot, or (In Review)
    never took one — so this only applies to a Blocked issue.

    **Sends first, unblocks second**: an agent that has
    not received the message is not awake yet, and a send that failed leaves
    the issue BLOCKED, with no rollback path to get wrong. The run slot is
    given back either way, once the send has had its chance
    (:func:`_release_run_slot`).
    """
    issue = store.get(key)
    if issue is None or issue.status not in IN_FLIGHT or issue.agent is None:
        status = issue.status if issue is not None else "not tracked"
        logger.warning(
            "dropped a message for %s: no agent to deliver to (%s)", key, status
        )
        return
    waking = issue.status is IssueStatus.BLOCKED
    if waking:
        if not _await_run_slot(key):
            logger.warning(
                "dropped a message for %s: no longer blocked while waiting for"
                " a run slot",
                key,
            )
            return
        issue = store.get(key)
        if issue is None or issue.agent is None:
            _release_run_slot(key)
            return
    sent = send_now(issue.agent, message)
    if waking:
        if sent:
            store.unblock(key)
        _release_run_slot(key)


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
        "provider": issue.provider,
        "blocker": issue.blocker,
        # Empty rather than null for an operator's issue: the record is a flat
        # map of strings the CLI prints, and "no parent" prints as nothing.
        "parent": issue.parent or "",
    }


def brief_for(key: str, mode: Mode, provider: Provider) -> str:
    """The opening message an agent is given for issue ``key``.

    Naming the skill leaves the lifecycle in one place — the skill — instead
    of half-restating it here, where the two would drift. How it is named is
    the harness's own: a slash command in Claude Code, a sentence in Codex,
    which is why the wording comes from ``provider`` rather than from here.

    The mode rides along because it is the bridge's answer, not the agent's to
    look up: it is read off the repo's git remotes
    (:func:`foregent.workspaces.mode_for`), and the same answer decides
    whether the bridge advances ``main`` when the issue completes.
    """
    return harness_for(provider).brief(key, mode)


def mode_of(issue: Issue) -> Mode:
    """How ``issue``'s project wants its work landed (§6.4).

    Derived from the repo on every call rather than stored, so the brief an
    agent is given and the completion it is held to cannot disagree
    (:func:`foregent.workspaces.mode_for`).

    **An issue with no repo is bootstrap**, and the guard is not a formality:
    ``mode_for`` takes a path, ``Path("")`` is the current directory, and the
    bridge's own working directory is a checkout of foregent — which has an
    origin on GitHub and would answer Pull Request for an issue that names no
    repo at all. A restart recovers a repo for every agent sitting in a
    workspace and none for an agent running in a plain directory
    (:func:`rebuild_store`), and bootstrap is the right answer for that agent
    anyway: a project foregent cannot make a workspace in is one it cannot
    open a pull request for either.
    """
    if not issue.repo:
        return Mode.BOOTSTRAP
    return workspaces.mode_for(Path(issue.repo))


def agent_mcp_servers() -> dict[str, McpServer]:
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
    return {"foregent": McpServer(url=f"{config.api_url()}/mcp")}


def check_agent_mcp() -> None:
    """Warn if the box cannot give its agents Linear and GitHub (JIM-93).

    Agents inherit these from the machine, so an unprovisioned box dispatches
    agents that cannot read the issue they were sent to work — expensively,
    and only discovered once one is already running. A warning rather than a
    refusal: the fix is `foregent setup`, and a bridge that will not start is
    a worse way to say so.

    Once per harness, because each keeps its own config. A box that will only
    ever queue one harness is warned about the other, which is the cheaper of
    the two mistakes: the fix is one command, and a bridge silent about a
    harness nobody provisioned says nothing until a dispatch has already been
    paid for.
    """
    for provider in Provider:
        absent = sorted(set(mcp_servers.SERVERS) - mcp_servers.configured(provider))
        if absent:
            logger.warning(
                "%s MCP not configured for %s on this machine; run `foregent setup`",
                ", ".join(absent),
                provider,
            )
    for variable in mcp_servers.missing_credentials():
        logger.warning("%s is not set; agents cannot authenticate with it", variable)


def ensure_skills(provider: Provider) -> None:
    """Install every packaged skill for ``provider``, before a launch.

    The agent is briefed from the copy on disk, so dispatch writes the
    packaged text over whatever is there (JIM-143). A box where `foregent
    setup` was never run, or not re-run since an upgrade, would otherwise
    brief every agent from a skill foregent no longer ships, and nothing
    downstream can tell that it did. The cost is that a hand-edited skill does
    not survive a dispatch; edit the packaged one.

    **Must complete before `manager.launch`, never alongside it.** Claude Code
    watches skill directories live, but only ones that existed when the
    session started: on a fresh box with no `~/.claude/skills/`, a skill
    written after the agent starts is invisible to that agent for its whole
    life.

    Best-effort. A box foregent cannot write skills to still gets its agent,
    working the issue without foregent's lifecycle instructions, which beats
    not dispatching at all.

    Only the harness the issue will be worked on. Refreshing every one at
    every dispatch would write files no agent about to start will read.
    """
    try:
        outcomes = skills.install(provider=provider)
    except OSError as exc:
        logger.warning("could not install foregent's skills for %s: %s", provider, exc)
        return
    for name, outcome in outcomes:
        if outcome is not skills.Outcome.UNCHANGED:
            logger.info(
                "%s the %s skill in %s", outcome, name, skills.skills_root(provider)
            )


def dispatch() -> None:
    """Launch agents for the queued issues, capacity allowing (JIM-151).

    Launches until the queue is empty or the next issue does not fit, so one
    completion can start more than one agent where the queue has been waiting
    on capacity.

    **Strictly FIFO.** A head that does not fit stalls the queue rather than
    being skipped: skipping it would make queue order a scheduling policy, with
    a starvation question attached, and the only arrangement it helps is a box
    hosting two projects — which the one-project-per-box boundary
    (docs/ARCHITECTURE.md §1.1) says does not exist.

    Serialised by :data:`_dispatching`, because the capacity check and the
    write that satisfies it are several harness calls apart.

    Before launch, each issue is claimed directly in Linear (assignee + In
    Progress state) — no agent runs without a durable ownership record. On a
    Linear or harness failure the issue stays Queued, the rest of the queue is
    left alone, and the caller's request fails with 502. Foregent's skills are
    refreshed first (:func:`ensure_skills`), because the agent cannot pick up
    a skill that appears or changes after it starts.

    Dispatch is not atomic, and the deterministic agent label is what makes
    that survivable. If the brief fails to send after the agent starts, a
    retry finds the existing agent by label and adopts it instead of running
    a second one for the same issue. If the claim succeeds but the launch
    fails, Linear is left In Progress while the store keeps the issue Queued;
    that self-heals on retry, because claiming is idempotent — the durable
    fix for both is orphan reconciliation.
    """
    with _dispatching:
        while _dispatch_one():
            pass


def admits(issue: Issue) -> bool:
    """Whether there is room to launch ``issue`` now (JIM-248, JIM-250).

    Two counts, not one. **Live** is memory and disk: every in-flight issue
    holds one, parked ones included (§1.7) — a blocked agent is a live process
    holding a workspace and a pane, and freeing its slot would launch a second
    agent onto the same machine's back. **Active** is cores: only an issue
    actually being worked holds one, so a parked agent gives its own back
    while it waits.

    **Both limits are the box's, whatever the mode.** Live is bounded by what
    the box is told it can carry (:func:`foregent.config.max_agents`) and
    active by what it is told it can run at once
    (:func:`foregent.config.max_active`); nothing in the repository narrows
    either. In Pull Request mode each agent pushes its own branch and ``main``
    is the reviewer's to move. In bootstrap mode two agents branch from the
    same ``main``, and what makes that safe is the landing path rather than
    anything here: completions are serialised and the bookmark only moves
    fast-forward (:func:`foregent.workspaces.advance`), so the second agent to
    finish is refused, and rebasing onto the ``main`` it can now see is what
    lands it (JIM-252).

    **A sub-issue skips the live limit and is gated on the run limit alone**
    (§5.2, JIM-250). It was queued by a worker that is about to park on it,
    and a parked parent holds a live slot while giving back its run slot — so
    charging its children the live limit too would let a parent wait forever
    on children the limit it filled cannot admit. Bounding what a parent
    queues is the parent's job; bounding how many parents run is the
    operator's.

    **A wake waiting on a run slot is served before a fresh launch takes
    one** ("wake before fork"): while :data:`_waking` holds any key, this
    refuses rather than race a drainer already waiting in
    :func:`_await_run_slot` for the same slot — a parked agent already holds
    the scarce resource, and finishing it is what frees it.
    """
    if issue.parent is None:
        live = sum(1 for tracked in store if tracked.status in IN_FLIGHT)
        if live >= config.max_agents():
            return False
    with _run_slots:
        return not _waking and _active() < config.max_active()


def _dispatch_one() -> bool:
    """Launch an agent for the oldest Queued issue; whether one started.

    The caller holds :data:`_dispatching`.
    """
    issue = store.next_queued()
    if issue is None or not admits(issue):
        return False
    # The mode is read off the repo rather than the workspace: a secondary
    # workspace shares the repo's remotes, and an adopted agent's dispatch
    # never built one to read.
    mode = mode_of(issue)
    label = label_for(issue.key)
    repo = Path(issue.repo)
    provider = issue.provider
    ensure_skills(provider)
    # Claimed for the whole launch (JIM-248): `admits` and the store write
    # below, which is what makes this issue count toward `_active` on its own,
    # are several harness calls apart, and a wake reading that gap as a free
    # run slot would overshoot the limit. Given back either way, once the
    # write has had its chance or the launch has failed.
    with _run_slots:
        _claimed.add(issue.key)
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
            cwd = str(workspaces.create(repo, issue.key, provider))
            ref = manager.launch(
                LaunchSpec(
                    label=label,
                    cwd=cwd,
                    provider=provider,
                    model=issue.model,
                    mcp_servers=agent_mcp_servers(),
                )
            )
        manager.send(ref, brief_for(issue.key, mode, provider))
        store.add(
            replace(issue, status=IssueStatus.IN_PROGRESS, directory=cwd, agent=ref)
        )
    except linear.LinearError as exc:
        raise HTTPException(status_code=502, detail=f"Linear claim: {exc}") from exc
    except workspaces.WorkspaceError as exc:
        raise HTTPException(status_code=502, detail=f"workspace: {exc}") from exc
    except AgentError as exc:
        raise HTTPException(status_code=502, detail=f"agent harness: {exc}") from exc
    finally:
        _give_back_run_slot(issue.key)
    return True


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


@app.get("/health")
def health() -> dict[str, str]:
    """Report when an authentic Linear delivery last arrived, or never.

    The one thing about this bridge an operator cannot see from the issues:
    every agent is woken by push and nothing else, so a webhook that has
    stopped delivering leaves a fleet that looks merely idle. A timestamp
    hours old says which it is.
    """
    return {"last_linear_delivery": _last_delivery}


@app.get("/issues")
def list_issues() -> list[dict[str, str]]:
    """Return the tracked issues as ``{key, title, status, blocker}`` records."""
    return [_record(issue) for issue in store.list_issues()]


@app.post("/issues/{key}/queue")
def queue_issue(
    key: str,
    directory: Annotated[str, Body(embed=True)],
    provider: Annotated[Provider, Body(embed=True)] = DEFAULT_PROVIDER,
    model: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, str]:
    """Queue issue ``key`` against the repo at ``directory``, dispatching if free.

    ``directory`` is the project, not the agent's cwd: dispatch builds a
    per-issue workspace from it and runs the agent there
    (:mod:`foregent.workspaces`).

    ``provider`` is which harness works it, and is the operator's to name
    (§1.3) — unlike the mode, which is read off the repo. A harness foregent
    does not run is refused here rather than at launch, where an issue would
    already have been claimed.

    ``model`` is which model that harness runs, and is likewise the
    operator's. Unset, the harness chooses its own default. It is not checked
    here: every harness has its own names for its models, and the harness is
    what refuses one it does not know.

    Refused while the issue already has a live agent — :data:`IN_FLIGHT`, plus
    Queued itself — because dispatch finds that agent under the issue's
    deterministic label and adopts it as-is (§4.1) rather than starting a new
    one, so a provider or model named here would be silently discarded rather
    than reaching an agent at all.
    """
    existing = store.get(key)
    if existing is not None and (
        existing.status is IssueStatus.QUEUED or existing.status in IN_FLIGHT
    ):
        raise HTTPException(
            status_code=409, detail=f"{key} is already {existing.status}"
        )
    issue = store.queue(key, directory, provider, model)
    dispatch()
    return _record(store.get(key) or issue)


@app.post("/issues/{key}/complete")
def complete_issue(key: str) -> dict[str, str]:
    """Mark ``key`` Done here and in Linear, dispatch what is queued, and return it.

    Closing in Linear is the other end of the claim (§4.1): the issue foregent
    moved to In Progress is the issue foregent moves out of it, so the status
    tells the truth in every mode rather than only where a merged pull request
    happens to close it (JIM-200). An agent that already ended the issue its
    own way — canceled, most often — keeps that answer
    (:func:`foregent.linear.close_issue`).

    Best-effort, because the work is landed and the agent is about to be torn
    down: a Linear that cannot be reached leaves a status an operator can fix,
    while failing here would strand a completed issue in flight.
    """
    issue = store.complete(key)
    try:
        linear.close_issue(key)
    except linear.LinearError as exc:
        logger.error("could not close %s in Linear: %s", key, exc)
    tell_parent(issue)
    # The completion above sticks even if dispatch 502s: the caller sees the
    # error, but the issue is Done and the next one stays Queued until a later
    # queue/complete triggers dispatch again. Retrying complete is safe.
    # `_release_run_slot` is what gives a wake already waiting first look at
    # the run slot this frees, ahead of a fresh launch (JIM-248).
    _release_run_slot()
    return _record(issue)


def tell_parent(issue: Issue) -> None:
    """Tell the issue that delegated ``issue`` that it has landed (JIM-250).

    A parent hands its sub-issues to the queue and parks on them, and this
    line is the whole of what it is told: one child, Done. It goes to every
    parent state a delivery reaches — a working parent reads it as its next
    prompt, a parked one is woken by it — so the parent decides for itself
    whether to queue the next wave, park again, or finish.

    Enqueued before the run slot this completion frees is given back, which
    gives the parent's drainer a head start on claiming it over the dispatch
    that follows ("wake before fork", §5.2). A head start is all it is:
    nothing orders the drainer thread against that dispatch, so a queued
    sibling can still take the slot first and the parent waits for the next.

    Best-effort, like the Linear close beside it: a parent foregent is not
    running is nothing to correct, and the work is landed either way.
    """
    if issue.parent is None:
        return
    try:
        deliver_issue(issue.parent, f"{issue.key} is Done.")
    except Exception:
        # Everything, not just the 409 for a parent with no agent: this runs
        # before the run slot is given back and before the caller tears the
        # agent down, so an exception escaping here would stall the queue and
        # leave a completed agent running.
        logger.exception("could not tell %s that %s landed", issue.parent, issue.key)


@app.post("/issues/{key}/block")
def block_issue(key: str, blocker: Annotated[str, Body(embed=True)]) -> dict[str, str]:
    """Mark issue ``key`` Blocked with ``blocker`` and return the record.

    **Block is sleep** (JIM-248): the agent keeps its live slot, parked alive
    in its workspace, but gives back its run slot — so, unlike the live slot,
    blocking can free room for a queued issue to launch.
    ``_release_run_slot`` is what dispatches, after giving a wake already
    waiting on the freed slot first look at it, ahead of a fresh launch.
    """
    issue = store.block(key, blocker)
    _release_run_slot()
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
    (:func:`drain`) and this route only enqueues. What the caller is told is
    therefore that the message is *accepted*, not that it has been read: the
    issue comes back as it stands, so a parked one still reads BLOCKED until
    :func:`send_queued` has sent and unblocked it.

    409 for an issue with no agent to prompt, checked here rather than on the
    drainer so an event with nowhere to go is answered instead of queued.
    Both halves of the guard are needed and neither implies the other: a Done
    issue keeps the agent ref of the agent foregent has since stopped, and
    ``block()`` upserts an unknown key, so an issue can carry a blocker with
    nothing behind it.

    The live slot does not change here, whatever the status: the agent has
    been holding it the whole time. Waking a Blocked one is a different
    matter for the run slot it gave up (JIM-248) — this route only enqueues,
    and it is :func:`send_queued`, on the drainer thread, that waits for one
    and dispatches once it has it.
    """
    issue = store.get(key)
    if issue is None or issue.status not in IN_FLIGHT or issue.agent is None:
        status = issue.status if issue is not None else "not tracked"
        raise HTTPException(
            status_code=409, detail=f"{key} has no agent to deliver to ({status})"
        )
    deliveries_for(key).put((message, time.monotonic()))
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


def queue_event(event: Event, viewer: str = "") -> None:
    """Queue ``event`` for the agent working the issue it is about, if any.

    The match and the drop, shared by both webhook routes: an event about no
    tracked issue, or one with nobody behind it, is logged and dropped rather
    than raised, because both routes answer 200 for it. The enqueue goes
    through :func:`deliver_issue` rather than the queue directly, so the
    live-agent guard stays decided in one place. The issue's status is read
    here only to word the prompt: a parked agent is being woken and a working
    one is not.

    ``viewer`` is foregent's own account id on the event's platform, and is
    how its own writes coming back are dropped. Only Linear has one to give:
    GitHub review comments foregent caused are already dropped in the mapping
    (:func:`~foregent.github.webhook_event`), which is what keeps a GitHub
    delivery from needing a Linear call to be matched. Completed workflows
    deliberately bypass that drop because their sender is the actor whose push
    triggered them.

    ``MAIN_ADVANCED`` is the one kind that names no issue, and is handed to
    :func:`wake_on_push` instead of matched. It is branched on here rather
    than in the route because this is where both routes join, and a second
    join would be a second place for the drop and the enqueue to disagree.
    """
    if event.kind is EventKind.MAIN_ADVANCED:
        wake_on_push(event)
        return
    key = wakes(event, viewer)
    if not key:
        logger.debug(
            "dropping a %s event: %s",
            event.kind,
            "foregent's own write" if event.issue_key else "it names no issue",
        )
        return
    issue = store.get(key)
    parked = issue is not None and issue.status is IssueStatus.BLOCKED
    message = delivery_message(event, parked=parked)
    try:
        deliver_issue(key, message)
    except HTTPException as exc:
        logger.debug("event on %s reached nobody: %s", key, exc.detail)
        return
    logger.info("queued for %s on activity by %s", key, event.author or "someone")
    logger.debug("delivered to %s: %s", key, message)


def wake_on_push(event: Event) -> None:
    """Wake the agents parked on a pull request into the repo that moved.

    A push to ``main`` is about a repository, so who it reaches is decided
    here, from the issues, rather than by matching the payload
    (:func:`~foregent.events.wakes`). Three things make an issue one of them,
    and none of it is remembered from anywhere:

    - **Blocked**, because a working agent is told to check ``main`` before it
      pushes and does not need telling twice (JIM-167).
    - **Pull Request mode**, because a bootstrap agent has no pull request to
      go stale and no remote that could have moved under it.
    - **The repo that was pushed to**, which an issue names as a local path
      and the payload as ``owner/name``; ``origin`` joins the two
      (:func:`~foregent.workspaces.remote_slug`).

    **A repo whose slug cannot be read is woken anyway.** The failure is
    unreadable remotes, not a wrong answer, and a spurious wake costs one
    agent turn while a missed one leaves an agent parked forever on a base
    that has moved.

    Nothing is remembered about which workers pushed a pull request, and that
    is deliberate: the bridge holds no GitHub client to rebuild such a record
    with, so it would be empty after every restart — which is the ordinary
    case here, the operator merging a pull request and restarting on the
    change (§5.4). A worker parked on something else is woken too, reads one
    line and parks again; that is the whole price of not keeping it.
    """
    woken = 0
    for issue in store.in_flight():
        if issue.status is not IssueStatus.BLOCKED:
            continue
        if mode_of(issue) is not Mode.PULL_REQUEST:
            continue
        slug = workspaces.remote_slug(Path(issue.repo))
        if slug and slug != event.repo:
            continue
        try:
            deliver_issue(issue.key, delivery_message(event, parked=True))
        except HTTPException as exc:
            logger.debug("%s was not woken by the push: %s", issue.key, exc.detail)
            continue
        logger.info("woke %s: main advanced in %s", issue.key, event.repo)
        woken += 1
    if not woken:
        logger.debug("main advanced in %s: no parked agent to wake", event.repo)


@app.post("/webhooks/linear")
async def linear_webhook(request: Request) -> dict[str, str]:
    """Deliver what Linear pushes to the agent it is for.

    Push is the whole of foregent's inbound path: authenticate, map the
    payload to an :class:`~foregent.events.Event`, and hand it to
    :func:`queue_event` for the agent working the issue it names.

    **A delivery foregent does nothing with is still a success.** Most of what
    Linear sends is about issues no agent here is working, and a 200 is the
    honest answer: nothing failed, and telling Linear otherwise buys three
    pointless retries of an event that would be dropped again. That covers a
    payload naming no issue, an issue nobody is working, foregent's own
    writes coming back at it, and a repeat of a delivery already acted on —
    which is the same answer the first copy got, and the answer that stops
    Linear sending a third.

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
    signed body that is not JSON, which is not a delivery Linear makes, and
    for one whose own timestamp puts it outside the replay window
    (:func:`~foregent.linear.webhook_fresh`) — the signature holds, so
    refusing it is the whole of not acting on a replay.
    """
    body = await request.body()
    signature = request.headers.get(linear.SIGNATURE_HEADER, "")
    try:
        authentic = linear.webhook_authentic(body, signature)
    except linear.LinearError as exc:
        logger.error("cannot authenticate Linear webhooks: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not authentic:
        raise HTTPException(status_code=401, detail="signature does not match")
    payload = _payload(body)
    logger.debug(
        "Linear delivered a %s %s",
        payload.get("action") or "nameless",
        payload.get("type") or "entity",
    )
    if not linear.webhook_fresh(payload):
        raise HTTPException(status_code=400, detail="delivery is outside the window")
    global _last_delivery
    _last_delivery = datetime.now(UTC).isoformat(timespec="seconds")
    # Nothing is awaited between the read and the write, so two copies of one
    # delivery arriving together cannot both find it unseen.
    if signature in _recent:
        logger.info("dropping a repeat of a delivery already acted on")
        return {"status": "ok"}
    _recent.append(signature)
    event = linear.webhook_event(payload)
    if event is None:
        logger.debug("Linear webhook is about no issue foregent knows: %s", payload)
        return {"status": "ok"}
    try:
        viewer = await run_in_threadpool(own_viewer)
    except linear.LinearError as exc:
        logger.error("cannot tell foregent's own writes apart: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    queue_event(event, viewer)
    return {"status": "ok"}


@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict[str, str]:
    """Deliver what GitHub pushes to the agent whose pull request it is about.

    The same three steps as the Linear route — authenticate, map, queue — over
    a payload only :mod:`foregent.github` understands. **The pull request is
    resolved back to its issue through its head branch**, which Linear names
    after the issue and links the pull request by, so a worker never has to
    report its own pull request number to be findable.

    Foregent's own account id is not needed here, and no Linear call is made:
    a review comment caused by the agent that opened the pull request is
    dropped in the mapping, where the payload names both sides of that
    comparison. Completed workflows bypass that comparison because the sender
    is normally the agent whose push triggered CI. A comment in the
    conversation tab is the one delivery whose payload names no branch, and
    mapping one asks GitHub for it.

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
    logger.debug("GitHub delivered a %s %s", payload.get("action") or "nameless", kind)
    # Threadpooled because a conversation comment names no branch, so mapping
    # one asks GitHub for the pull request over a blocking socket.
    event = await run_in_threadpool(github.webhook_event, payload, kind)
    if event is None:
        logger.debug("GitHub delivered a %s event foregent has no use for", kind)
        return {"status": "ok"}
    # Threadpooled because a push reads each parked issue's remotes to find
    # out whose repo moved (:func:`wake_on_push`), and jj is a subprocess.
    # Enqueuing is the only thing that happens after that, so answering is
    # still not waiting on any agent.
    await run_in_threadpool(queue_event, event)
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
    # or more, and this tool handler is a coroutine on the event loop, so it
    # must be threadpooled to avoid stalling the whole server.
    try:
        await run_in_threadpool(complete_issue, issue_key)
        result = f"Marked {issue_key} complete."
    except HTTPException as exc:
        # store.complete already succeeded; only the follow-on dispatch
        # failed, and retrying complete is safe (server.py's /complete route
        # docstring) — so report success rather than raising a tool error.
        result = f"Marked {issue_key} complete; next dispatch failed: {exc.detail}"
    # The issue is Done, so nothing more is delivered to it and its drainer
    # can end. Anything still queued drains first and is dropped by
    # send_queued, which is what a message for a finished agent deserves.
    stop_deliveries(issue_key)
    if issue is not None:
        result += await run_in_threadpool(teardown, issue)
    return result


def teardown(issue: Issue) -> str:
    """Stop ``issue``'s agent, remove its workspace, and say what went wrong.

    **One call, on one thread, and that is the whole point.** Stopping the
    agent severs the connection it called :func:`complete_task` over, and the
    tool is served on a stateless streamable-HTTP session, whose request
    handler is cancelled when its client disconnects — so an ``await`` after
    the stop is never reached. A thread already running is not interrupted,
    so a teardown that begins here finishes even though the coroutine that
    started it does not (JIM-150).

    Stopping the agent is best-effort, because the issue is already Done. A
    failure ends the teardown there rather than going on to the directory:
    removing a live agent's own cwd out from under it is worse than leaving
    one behind.

    The workspace goes second for that same reason. The work is in git by
    now, so a failure costs a directory rather than the issue's commits, but
    it is still reported rather than passed over, because nobody owns the
    leftovers.

    Returns what to append to the tool's answer: empty when both halves
    worked.
    """
    if issue.agent is not None:
        try:
            manager.stop(issue.agent)
        except AgentError as exc:
            return f" Agent teardown failed: {exc}"
    if issue.repo and issue.directory:
        try:
            workspaces.destroy(Path(issue.repo), issue.key, Path(issue.directory))
        except workspaces.WorkspaceError as exc:
            logger.error("could not remove the %s workspace: %s", issue.key, exc)
            return f" The workspace was left behind ({exc})."
    return ""


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
    descended from it, which is the ordinary race between concurrent
    bootstrap agents (JIM-252): ``main`` moves whenever one of them lands,
    which can be after this agent's last rebase. The refused agent is alive,
    so the message tells it what to do — rebase onto ``main``, resolve any
    conflicts, and call ``complete_task`` again. Returning it leaves the issue
    in flight and the workspace on disk, which is what makes the retry
    possible: the commits exist only in that workspace, and going on would
    tear it down and take them with it.

    Only an in-flight issue is landed, which is what keeps completing twice
    safe. The second call has no workspace left to name a revision in, and jj
    would refuse the move for a reason that says nothing about the work.
    """
    if issue is None or issue.status not in IN_FLIGHT or not issue.repo:
        return None
    if mode_of(issue) is not Mode.BOOTSTRAP:
        return None
    repo = Path(issue.repo)
    try:
        await run_in_threadpool(workspaces.advance, repo, issue_key)
    except workspaces.WorkspaceError as exc:
        logger.error("could not advance %s for %s: %s", workspaces.TRUNK, issue_key, exc)
        return (
            f"{issue_key} was not completed: {workspaces.TRUNK} could not be "
            f"moved onto its work ({exc}). The workspace is still there, and "
            f"the commits are only in it. If {workspaces.TRUNK} moved on "
            f"after this work was last rebased, or the rebase left conflicts, "
            f"that is the cause: rebase onto {workspaces.TRUNK}, resolve every "
            f"conflict, and call complete_task again."
        )
    return None


@mcp.tool()
async def queue_sub_issues(issue_key: str, keys: list[str]) -> str:
    """Hand the sub-issues ``keys`` of ``issue_key`` to foregent's queue.

    Each key is queued at the back of the queue against the calling issue's
    own repo, harness and model, recorded as a sub-issue of ``issue_key``, and
    dispatched as capacity allows. A key foregent has already queued, is
    already running, or has already finished is refused and left alone, so
    calling this twice with the same wave costs nothing.

    The sub-issue link itself is not checked: **the caller is trusted** to
    have created these in Linear with ``parentId`` first. What is recorded
    here is who to wake, not what the Linear tree says.

    A sub-issue needs only a free run slot to launch, not one of the live
    slots a parked parent is holding, so children queued this way always have
    somewhere to run.
    """
    return await run_in_threadpool(_queue_sub_issues, issue_key, keys)


def _queue_sub_issues(issue_key: str, keys: list[str]) -> str:
    """Queue ``keys`` under ``issue_key`` and dispatch; what to tell the caller.

    Blocking: :func:`dispatch` launches agents, which is minutes of harness
    calls, so the tool above runs this on a thread rather than the event loop.
    """
    parent = store.get(issue_key)
    if parent is None:
        return f"Nothing was queued: {issue_key} is not an issue foregent is tracking."
    # A child is queued against its parent's repo, and an empty one is not a
    # repo: `Path("")` is the bridge's own working directory, so dispatch
    # would build the workspace in foregent's checkout. An adopted agent
    # whose cwd was not a workspace is recovered with no repo (`_adopted`),
    # and this is the one path by which such an issue could reach the queue.
    if not parent.repo:
        return (
            f"Nothing was queued: foregent has no repo recorded for {issue_key},"
            " so it cannot build a workspace for a sub-issue. Ask the operator"
            " to re-queue this issue against its repo."
        )
    queued: list[str] = []
    refused: list[str] = []
    for key in keys:
        existing = store.get(key)
        # Done as well as live: a child that has already landed is re-claimed
        # in Linear and its work redone if it is queued again, and a parent
        # told to "queue the next wave" is one turn away from re-sending its
        # whole list. Orphaned is deliberately absent — re-queueing is what
        # an agent that died deserves.
        if existing is not None and (
            existing.status
            in (IssueStatus.QUEUED, IssueStatus.DONE)
            or existing.status in IN_FLIGHT
        ):
            refused.append(f"{key} ({existing.status})")
            continue
        store.queue(key, parent.repo, parent.provider, parent.model, parent=issue_key)
        queued.append(key)
    lines = [
        f"Queued as sub-issues of {issue_key}: {', '.join(queued)}."
        if queued
        else f"Queued nothing for {issue_key}."
    ]
    if refused:
        lines.append(
            f"Already queued, running or finished, so left alone and not yours"
            f" to wait for: {', '.join(refused)}."
        )
    try:
        dispatch()
    except HTTPException as exc:
        # The queue itself stands; only the launch failed, and the next queue
        # or completion dispatches again.
        lines.append(f"Dispatch failed, so they wait in the queue: {exc.detail}.")
    if queued:
        # Only what this call actually queued will wake this caller. A refused
        # key keeps whatever parent it already had, so telling a caller that
        # queued nothing to park on it would park it on a wake that goes
        # somewhere else.
        lines.append(
            "Park with report_blocked naming them once you have nothing to do"
            " until they land; each one that lands wakes you."
        )
    return " ".join(lines)


@mcp.tool()
async def report_blocked(issue_key: str, blocker: str) -> str:
    """Record ``blocker`` on ``issue_key`` in the foregent issue store.

    The agent stays parked alive in its workspace: this only records state,
    nothing is terminated, and there is no wake mechanism here (the bridge
    prompts the agent with the resolving event later).
    """
    await run_in_threadpool(block_issue, issue_key, blocker)
    return f"Recorded blocker {blocker!r} on {issue_key}."


def mcp_host() -> str:
    """The host an agent's MCP client will name in its ``Host`` header.

    mcp answers a request whose ``Host`` it does not expect with 421, as DNS
    rebinding protection, and the allowlist it builds is derived from this.
    Agents reach the bridge at :func:`config.api_url`, so that URL's host is
    the one to declare: on the default loopback URL that keeps the protection
    on, and a bridge published under any other name is reachable rather than
    rejecting every agent that calls it.
    """
    return urlparse(config.api_url()).hostname or "127.0.0.1"


# Mounted at "/" (not "/mcp") because streamable_http_app() already routes at
# its own streamable_http_path (default "/mcp") — mounting it at "/mcp" would
# yield "/mcp/mcp". Mounted last so the explicit REST routes above take
# precedence and this catch-all doesn't shadow them. Calling
# streamable_http_app() here (import time) is also what creates
# `mcp.session_manager` lazily, which `lifespan` above depends on.
# stateless_http: these tools are fire-and-forget, so session-id bookkeeping
# would be pure overhead.
app.mount("/", mcp.streamable_http_app(stateless_http=True, host=mcp_host()))
