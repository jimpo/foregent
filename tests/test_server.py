"""Tests for the bridge's dispatch path (JIM-88).

The server is driven through a fake :class:`~foregent.agents.AgentManager`,
which is the point of the seam: none of this knows what a herdr or a Claude
Code is. Linear is stubbed too — its own client is covered by
``tests.test_linear_integration``.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from unittest import mock

from foregent import herdr, server
from foregent.agents import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
)
from foregent.models import Issue, IssueStatus, Mode
from foregent.store import IN_FLIGHT, IssueStore
from foregent.workspaces import WorkspaceError


class FakeManager:
    """An agent harness that records what the bridge asked it to do."""

    def __init__(self, existing: list[AgentRecord] | None = None) -> None:
        self.launched: list[LaunchSpec] = []
        self.sent: list[tuple[AgentRef, str]] = []
        # Whether each send asked to be held until the agent was free.
        self.idle_gated: list[bool] = []
        self.stopped: list[AgentRef] = []
        self.existing = existing or []
        self.stream: list[AgentEvent] = []
        self.fail_launch: Exception | None = None
        self.fail_send: Exception | None = None
        self.fail_status: Exception | None = None
        # How many sends fail before one lands, negative for all of them.
        # The delivery drainer keeps offering a refused message, so a harness
        # that recovers and one that never does are both worth driving
        # (JIM-132).
        self.fail_sends = -1
        self.agent_status = AgentStatus.IDLE
        # Observes the world as the agent starts, for the things dispatch has
        # to have finished by then rather than merely around then.
        self.at_launch: Callable[[], None] | None = None
        # The same, for teardown: what must still be standing while the agent
        # is being stopped.
        self.on_stop: Callable[[], None] | None = None

    def describe(self) -> str:
        return "a fake harness"

    def launch(self, spec: LaunchSpec) -> AgentRef:
        if self.at_launch:
            self.at_launch()
        if self.fail_launch:
            raise self.fail_launch
        self.launched.append(spec)
        ref = AgentRef(spec.label, "conversation-1")
        self.existing.append(AgentRecord(ref, AgentStatus.IDLE, spec.cwd))
        return ref

    def send(self, ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
        self.idle_gated.append(when_idle)
        if self.fail_send and self.fail_sends != 0:
            self.fail_sends -= 1
            raise self.fail_send
        self.sent.append((ref, text))

    def status(self, ref: AgentRef) -> AgentStatus:
        if self.fail_status:
            raise self.fail_status
        return self.agent_status

    def wait(
        self, ref: AgentRef, until: Collection[AgentStatus], timeout: float
    ) -> AgentStatus:
        return AgentStatus.IDLE

    def read(self, ref: AgentRef, lines: int = 50) -> str:
        return ""

    def stop(self, ref: AgentRef) -> None:
        if self.on_stop:
            self.on_stop()
        self.stopped.append(ref)

    def list_agents(self) -> list[AgentRecord]:
        return list(self.existing)

    def events(self) -> Iterator[AgentEvent]:
        return iter(self.stream)


def drain_events(manager: FakeManager) -> None:
    """Run the event consumer against ``manager`` until its stream runs out."""
    with mock.patch.object(server, "manager", manager):
        server.watch_agents()
        # The consumer runs in a daemon thread; the fake stream is finite,
        # so it ends on its own.
        for thread in threading.enumerate():
            if thread.name == "foregent-agent-events":
                thread.join(timeout=5)


def drain_deliveries() -> None:
    """Wait until every queued delivery has been handled.

    The drainers start themselves on the first delivery to an issue, so this
    only waits. Bounded, because a drainer is endless by design: a delivery
    that never finishes fails the test instead of hanging it.
    """
    for pending in list(server.deliveries.values()):
        waiter = threading.Thread(target=pending.join, daemon=True)
        waiter.start()
        waiter.join(timeout=5)


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        patcher = mock.patch.object(server, "manager", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        claim = mock.patch.object(server.linear, "claim_issue")
        self.claim = claim.start()
        self.addCleanup(claim.stop)
        # Dispatch installs foregent's skills into the box's Claude Code
        # config directory; point that somewhere disposable so running the
        # tests never writes into the real ~/.claude.
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})
        )
        self.skill = self.config / "skills" / "foregent-worker" / "SKILL.md"

    def queue(self, key: str = "JIM-88", directory: str = "/ws/JIM-88") -> None:
        server.store.queue(key, directory)

    def test_dispatch_claims_before_launching(self) -> None:
        # Nothing runs without a durable ownership record in Linear .
        self.queue()
        server.dispatch()
        self.claim.assert_called_once_with("JIM-88")
        self.assertEqual(len(self.manager.launched), 1)

    def test_dispatch_runs_a_plain_directory_agent_in_it_directly(self) -> None:
        # A queued directory foregent cannot make a workspace from is used as
        # the cwd as it stands, so a non-jj project still gets its agent.
        self.queue()
        server.dispatch()
        spec = self.manager.launched[0]
        self.assertEqual(spec.cwd, "/ws/JIM-88")
        self.assertEqual(spec.label, "fg-jim-88")

    def test_dispatch_gives_the_agent_foregents_own_tools(self) -> None:
        # Without these an agent cannot report itself blocked or done, so the
        # bridge never learns the outcome of the work it dispatched.
        self.queue()
        server.dispatch()
        spec = self.manager.launched[0]
        self.assertIn("foregent", spec.mcp_servers)
        self.assertTrue(spec.mcp_servers["foregent"]["url"].endswith("/mcp"))

    def test_dispatch_leaves_the_machines_mcp_config_in_place(self) -> None:
        # Agents reach Linear and GitHub through the machine's own
        # configuration, which `foregent setup` provisions (JIM-93). Strict
        # mode would cut them off from the issue tracker entirely, and would
        # buy nothing the box does not already give the operator's sessions.
        self.queue()
        server.dispatch()
        self.assertFalse(self.manager.launched[0].strict_mcp)

    def test_dispatch_briefs_the_agent_and_records_it(self) -> None:
        self.queue()
        server.dispatch()
        ref, text = self.manager.sent[0]
        self.assertIn("JIM-88", text)
        # The brief names the skill outright: an agent that never loads it
        # does the work and then stops, telling foregent nothing.
        self.assertIn("foregent-worker", text)
        # And names the mode, so the agent is told how to land the work rather
        # than left to work it out (JIM-152). A queued directory that is no jj
        # repo has no remote to read, which is bootstrap.
        self.assertEqual(text, "/foregent-worker JIM-88 bootstrap")
        issue = server.store.get("JIM-88")
        assert issue is not None and issue.agent is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)
        self.assertEqual(issue.agent, ref)
        # The conversation id is the half that outlives the process.
        self.assertEqual(issue.agent.conversation_id, "conversation-1")
        # The brief is the one send that waits for a free agent: it is the
        # agent's assignment, and landing it mid-turn would interrupt work
        # it cannot have been given yet.
        self.assertEqual(self.manager.idle_gated, [True])

    def test_dispatch_installs_a_missing_skill_before_the_agent_starts(self) -> None:
        # Claude Code only watches skill directories that existed when the
        # session started, so a skill written after agent.start is invisible
        # to that agent for its whole life (JIM-91).
        present_at_launch: list[bool] = []
        self.manager.at_launch = lambda: present_at_launch.append(self.skill.is_file())
        self.queue()
        server.dispatch()
        self.assertEqual(present_at_launch, [True])
        self.assertIn("foregent-worker", self.skill.read_text())

    def test_dispatch_leaves_an_existing_skill_alone(self) -> None:
        # The safety net only fills gaps: `foregent setup` is the one
        # deliberate updater, so an operator's edit survives every dispatch.
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("hand edited\n")
        self.queue()
        server.dispatch()
        self.assertEqual(self.skill.read_text(), "hand edited\n")

    def test_dispatch_survives_a_skill_directory_it_cannot_write(self) -> None:
        # An agent working the issue without foregent's instructions beats no
        # agent at all, so a broken skill directory must not block dispatch.
        (self.config / "not-a-directory").write_text("")
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.config / "not-a-directory")}
        ):
            self.queue()
            server.dispatch()
        self.assertEqual(len(self.manager.launched), 1)

    def test_a_failed_claim_leaves_the_issue_queued(self) -> None:
        self.claim.side_effect = server.linear.LinearError("no such issue")
        self.queue()
        with self.assertRaises(server.HTTPException) as caught:
            server.dispatch()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(len(self.manager.launched), 0)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.QUEUED)

    def test_a_failed_launch_leaves_the_issue_queued(self) -> None:
        self.manager.fail_launch = AgentError("herdr is down")
        self.queue()
        with self.assertRaises(server.HTTPException) as caught:
            server.dispatch()
        self.assertEqual(caught.exception.status_code, 502)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.QUEUED)

    def test_a_retry_adopts_the_agent_a_failed_attempt_left_running(self) -> None:
        # Dispatch is not atomic: an agent can be running while the store
        # still shows the issue Queued. The deterministic label is what makes
        # that survivable — the retry finds it rather than starting a second
        # agent for one issue.
        self.manager.fail_send = AgentError("prompt never landed")
        self.queue()
        with self.assertRaises(server.HTTPException):
            server.dispatch()
        self.manager.fail_send = None

        server.dispatch()
        self.assertEqual(len(self.manager.launched), 1)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_capacity_is_one_agent(self) -> None:
        self.queue("JIM-88", "/ws/JIM-88")
        server.dispatch()
        self.queue("JIM-89", "/ws/JIM-89")
        server.dispatch()
        self.assertEqual([spec.label for spec in self.manager.launched], ["fg-jim-88"])

    def test_a_parked_agent_still_holds_its_slot(self) -> None:
        # A blocked agent is alive in its workspace, so it must keep occupying
        # capacity.
        self.queue()
        server.dispatch()
        server.store.block("JIM-88", "a review of the PR")
        self.queue("JIM-89", "/ws/JIM-89")
        server.dispatch()
        self.assertEqual(len(self.manager.launched), 1)

    def test_dispatch_waits_for_a_dispatch_already_running(self) -> None:
        # Dispatch reads the store for a free slot and writes the launched
        # agent back several harness calls later, so a second caller that ran
        # in that window would read a store nobody had written yet and launch
        # the same issue again. Holding the lock here stands in for the first
        # caller being mid-launch.
        self.queue()
        launched = threading.Event()

        with server._dispatching:
            threading.Thread(
                target=lambda: (server.dispatch(), launched.set()), daemon=True
            ).start()
            self.assertFalse(launched.wait(0.2))
            self.assertEqual(self.manager.launched, [])

        self.assertTrue(launched.wait(5))
        self.assertEqual([spec.label for spec in self.manager.launched], ["fg-jim-88"])

    def test_completing_an_issue_dispatches_the_next(self) -> None:
        self.queue()
        server.dispatch()
        self.queue("JIM-89", "/ws/JIM-89")
        server.complete_issue("JIM-88")
        self.assertEqual(
            [spec.label for spec in self.manager.launched], ["fg-jim-88", "fg-jim-89"]
        )


class ModeTests(unittest.TestCase):
    """The mode an issue is briefed in and completed in (JIM-151)."""

    def test_an_issue_with_no_repo_is_bootstrap_without_reading_a_repo(self) -> None:
        # `Path("")` is the current directory, so an unguarded lookup reads the
        # bridge's *own* checkout — foregent's, which has an origin on GitHub.
        def never(repo: Path) -> Mode:
            raise AssertionError(f"read the mode of {repo}")

        with mock.patch.object(server.workspaces, "mode_for", never):
            mode = server.mode_of(Issue(key="JIM-88", title=""))

        self.assertIs(mode, Mode.BOOTSTRAP)

    def test_an_issue_with_a_repo_is_the_mode_its_remotes_name(self) -> None:
        with mock.patch.object(
            server.workspaces, "mode_for", return_value=Mode.PULL_REQUEST
        ) as mode_for:
            mode = server.mode_of(Issue(key="JIM-88", title="", repo="/ws/repo"))

        self.assertIs(mode, Mode.PULL_REQUEST)
        mode_for.assert_called_once_with(Path("/ws/repo"))


class DeliverTests(unittest.TestCase):
    """Queueing an event for the agent it was for (JIM-131, JIM-132)."""

    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        # A fresh set of queues per test, so a drainer left over from an
        # earlier one cannot take this test's messages.
        self.enterContext(mock.patch.object(server, "deliveries", {}))
        # The drainer paces its retries against a real agent's turn; nothing
        # here is really busy.
        self.enterContext(mock.patch.object(server, "DELIVERY_RETRY_SECONDS", 0))
        self.ref = AgentRef("fg-jim-88", "conversation-1")

    def park(self, blocker: str = "a review of the PR") -> None:
        server.store.add(
            Issue(
                key="JIM-88",
                title="",
                status=IssueStatus.BLOCKED,
                blocker=blocker,
                agent=self.ref,
            )
        )

    def work(self, status: IssueStatus = IssueStatus.IN_PROGRESS) -> None:
        server.store.add(
            Issue(key="JIM-88", title="", status=status, agent=self.ref)
        )

    def issue(self) -> Issue:
        issue = server.store.get("JIM-88")
        assert issue is not None
        return issue

    def test_delivering_queues_rather_than_sending_on_the_callers_thread(self) -> None:
        # A send talks to the harness and is retried until it lands, and
        # Linear retries any webhook delivery the bridge is slow to answer,
        # so no ingesting caller may be held behind one (JIM-132).
        self.work()
        record = server.deliver_issue("JIM-88", "AJ commented: ship it")
        self.assertEqual(self.manager.sent, [])
        self.assertEqual(record["status"], IssueStatus.IN_PROGRESS)
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "AJ commented: ship it")])

    def test_a_parked_agent_is_unblocked_once_its_message_is_sent(self) -> None:
        self.park()
        record = server.deliver_issue("JIM-88", "AJ commented: ship it")
        # Accepted, not yet read: the issue moves when the agent has it.
        self.assertEqual(record["status"], IssueStatus.BLOCKED)
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "AJ commented: ship it")])
        self.assertEqual(self.issue().status, IssueStatus.IN_PROGRESS)
        self.assertEqual(self.issue().blocker, "")

    def test_a_working_agent_is_sent_to_and_left_as_it_was(self) -> None:
        # A worker sees activity on its own issue as it happens; it was never
        # waiting, so there is no status to move it out of.
        self.work()
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "AJ commented: ship it")])
        self.assertEqual(self.issue().status, IssueStatus.IN_PROGRESS)

    def test_a_working_agent_is_prompted_without_waiting_out_its_turn(self) -> None:
        # Delivery is not gated on the agent being free (JIM-144). Gating it
        # held every message for the whole turn — the harness waits five
        # minutes for an idle agent, fails, and is offered the message
        # again — so a worker heard nothing while it worked, and one whose
        # turn ended in `complete_task` was torn down having never read it.
        # The harness queues a prompt behind the turn in progress, so it is
        # submitted straight away.
        self.work()
        self.manager.agent_status = AgentStatus.WORKING
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        drain_deliveries()
        self.assertEqual(self.manager.idle_gated, [False])
        self.assertEqual(self.manager.sent, [(self.ref, "AJ commented: ship it")])

    def test_a_parked_agent_is_prompted_the_same_way(self) -> None:
        # Being parked is not a second delivery path: a parked agent is idle
        # anyway, and the wake reads the same as any other message.
        self.park()
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        drain_deliveries()
        self.assertEqual(self.manager.idle_gated, [False])

    def test_an_agent_in_review_is_sent_to_and_left_as_it_was(self) -> None:
        self.work(IssueStatus.IN_REVIEW)
        server.deliver_issue("JIM-88", "go on")
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "go on")])
        self.assertEqual(self.issue().status, IssueStatus.IN_REVIEW)

    def test_two_events_for_one_agent_keep_the_order_they_arrived_in(self) -> None:
        # Two people commenting during one long turn are two prompts, in the
        # order they were written; merging them would lose who said what.
        self.work()
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        server.deliver_issue("JIM-88", "Sam commented: hold on")
        drain_deliveries()
        self.assertEqual(
            [text for _, text in self.manager.sent],
            ["AJ commented: ship it", "Sam commented: hold on"],
        )

    def test_a_refused_message_is_offered_again_rather_than_dropped(self) -> None:
        # A harness that refuses a prompt says the agent is momentarily
        # unreachable, not that the message is lost: a hiccup is not a reason
        # to throw an event away (JIM-132).
        self.work()
        self.manager.fail_send = AgentError("agent is busy")
        self.manager.fail_sends = 2
        server.deliver_issue("JIM-88", "go on")
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "go on")])

    def test_a_gone_agent_drops_its_queued_events(self) -> None:
        # The one end to the wait: a message for an agent that no longer
        # exists can never land, so it is dropped and logged rather than
        # retried forever.
        self.park()
        self.manager.fail_send = AgentError("agent is gone")
        self.manager.agent_status = AgentStatus.GONE
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        server.deliver_issue("JIM-88", "Sam commented: hold on")
        with self.assertLogs(server.logger, "WARNING"):
            drain_deliveries()
        self.assertEqual(self.manager.sent, [])
        # Nothing was delivered, so nothing was woken: the blocker still
        # stands for the operator reading `foregent status`.
        self.assertEqual(self.issue().status, IssueStatus.BLOCKED)
        self.assertEqual(self.issue().blocker, "a review of the PR")

    def test_an_unreachable_harness_is_not_taken_for_a_dead_agent(self) -> None:
        # A harness that answers nothing about the agent is no proof it died,
        # and dropping the event on a blinking socket would lose it for good.
        self.work()
        self.manager.fail_send = AgentError("herdr is unreachable")
        self.manager.fail_sends = 1
        self.manager.fail_status = AgentError("herdr is unreachable")
        server.deliver_issue("JIM-88", "go on")
        drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "go on")])

    def test_an_agent_that_died_while_its_message_waited_is_not_sent_to(self) -> None:
        # The store is read again on the drainer: the consumer orphans the
        # issue when the agent exits, and a queued message must not be handed
        # to whatever holds that key next.
        self.work()
        server.deliver_issue("JIM-88", "go on")
        server.store.orphan("JIM-88")
        with self.assertLogs(server.logger, "WARNING"):
            drain_deliveries()
        self.assertEqual(self.manager.sent, [])

    def test_one_failed_delivery_does_not_strand_the_ones_behind_it(self) -> None:
        # There is a single drainer, so a message that kills it would leave
        # every message behind it waiting forever.
        self.work()
        self.manager.fail_send = RuntimeError("the drainer's own bug")
        self.manager.fail_sends = 1
        server.deliver_issue("JIM-88", "first")
        server.deliver_issue("JIM-88", "second")
        with self.assertLogs(server.logger, "ERROR"):
            drain_deliveries()
        self.assertEqual(self.manager.sent, [(self.ref, "second")])

    def test_an_unreachable_agent_does_not_hold_up_another_agents_messages(
        self,
    ) -> None:
        # One queue for the fleet meant the first stuck send silenced every
        # agent behind it, because a refused message is offered again until
        # its agent is gone (JIM-151).
        stuck = AgentRef("fg-jim-88", "conversation-1")
        free = AgentRef("fg-jim-89", "conversation-2")
        server.store.add(
            Issue(key="JIM-88", title="", status=IssueStatus.IN_PROGRESS, agent=stuck)
        )
        server.store.add(
            Issue(key="JIM-89", title="", status=IssueStatus.IN_PROGRESS, agent=free)
        )
        held = threading.Event()
        sent = self.manager.send

        def hold(ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
            if ref == stuck:
                held.wait(5)
            sent(ref, text, when_idle=when_idle)

        self.enterContext(mock.patch.object(self.manager, "send", hold))
        self.addCleanup(held.set)

        server.deliver_issue("JIM-88", "wedged")
        server.deliver_issue("JIM-89", "go on")

        waiter = threading.Thread(
            target=server.deliveries["JIM-89"].join, daemon=True
        )
        waiter.start()
        waiter.join(timeout=5)
        self.assertEqual(self.manager.sent, [(free, "go on")])

    def test_a_completed_issue_stops_its_drainer(self) -> None:
        self.work()
        server.deliver_issue("JIM-88", "AJ commented: ship it")
        drain_deliveries()

        server.stop_deliveries("JIM-88")

        self.assertEqual(server.deliveries, {})
        for thread in threading.enumerate():
            if thread.name == "foregent-deliveries-JIM-88":
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_delivering_does_not_dispatch_anything_else(self) -> None:
        # The agent held its capacity slot the whole time, parked or not , so
        # prompting it frees nothing.
        self.park()
        server.store.queue("JIM-89", "/ws/JIM-89")
        server.deliver_issue("JIM-88", "go on")
        drain_deliveries()
        self.assertEqual(self.manager.launched, [])

    def test_delivering_to_an_untracked_issue_is_a_conflict(self) -> None:
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(server.deliveries, {})

    def test_delivering_to_a_queued_issue_is_a_conflict(self) -> None:
        # Nothing is running yet: the brief at dispatch is what it will read.
        server.store.queue("JIM-88", "/ws/JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(server.deliveries, {})

    def test_delivering_to_an_orphaned_issue_is_a_conflict(self) -> None:
        self.work()
        server.store.orphan("JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(server.deliveries, {})

    def test_delivering_to_a_completed_issue_is_a_conflict(self) -> None:
        # Done keeps the ref of the agent foregent has since stopped, so the
        # status is what rules it out, not the missing agent.
        self.work()
        server.store.complete("JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(server.deliveries, {})

    def test_delivering_to_a_blocked_issue_with_no_agent_is_a_conflict(self) -> None:
        # `block()` upserts an unknown key, so an issue can carry a blocker
        # with nothing to prompt.
        server.store.block("JIM-88", "a review of the PR")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(server.deliveries, {})


class CheckHerdrProtocolTests(unittest.TestCase):
    """Refusing to start on a herdr protocol drift."""

    def check(self, client: mock.Mock) -> None:
        with mock.patch.object(server.herdr, "HerdrClient", return_value=client):
            server.check_herdr_protocol()

    def test_a_matching_protocol_does_not_raise(self) -> None:
        client = mock.Mock()
        self.check(client)
        client.check_protocol.assert_called_once()

    def test_a_protocol_drift_stops_startup(self) -> None:
        client = mock.Mock()
        client.check_protocol.side_effect = herdr.HerdrError("drift")
        with self.assertRaises(herdr.HerdrError):
            self.check(client)


class CheckAgentMCPTests(unittest.TestCase):
    """Saying at startup that agents will have no issue tracker (JIM-93)."""

    def test_an_unprovisioned_box_is_warned_about(self) -> None:
        with mock.patch.object(server.mcp_servers, "configured", return_value=set()):
            with mock.patch.dict(
                os.environ, {"LINEAR_API_KEY": "k", "GITHUB_TOKEN": "t"}
            ):
                with self.assertLogs(server.logger, "WARNING") as logs:
                    server.check_agent_mcp()
        self.assertIn("foregent setup", "".join(logs.output))

    def test_a_configured_server_with_no_credential_is_warned_about(self) -> None:
        # Configured but unauthenticated: the agent launches, then discovers
        # mid-issue that it cannot read Linear.
        with mock.patch.object(
            server.mcp_servers, "configured", return_value={"linear", "github"}
        ):
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k"}):
                os.environ.pop("GITHUB_TOKEN", None)
                with self.assertLogs(server.logger, "WARNING") as logs:
                    server.check_agent_mcp()
        self.assertIn("GITHUB_TOKEN", "".join(logs.output))

    def test_a_provisioned_box_says_nothing(self) -> None:
        with mock.patch.object(
            server.mcp_servers, "configured", return_value={"linear", "github"}
        ):
            with mock.patch.dict(
                os.environ, {"LINEAR_API_KEY": "k", "GITHUB_TOKEN": "t"}
            ):
                with self.assertNoLogs(server.logger, "WARNING"):
                    server.check_agent_mcp()


class RebuildStoreTests(unittest.TestCase):
    """Recovering the issue<->agent map from the harness."""

    def setUp(self) -> None:
        server.store = IssueStore()

    def rebuild(self, manager: FakeManager) -> None:
        with mock.patch.object(server, "manager", manager):
            server.rebuild_store()

    def test_live_agents_are_recovered_by_label(self) -> None:
        self.rebuild(
            FakeManager(
                [AgentRecord(AgentRef("fg-jim-88", "abc"), AgentStatus.IDLE, "/ws")]
            )
        )
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)
        self.assertEqual(issue.agent, AgentRef("fg-jim-88", "abc"))
        self.assertEqual(issue.directory, "/ws")

    def test_agents_foregent_did_not_launch_are_ignored(self) -> None:
        self.rebuild(
            FakeManager([AgentRecord(AgentRef("scratch"), AgentStatus.IDLE, "/tmp")])
        )
        self.assertEqual(len(server.store), 0)

    def test_an_unreachable_harness_does_not_block_startup(self) -> None:
        manager = FakeManager()
        manager.list_agents = mock.Mock(side_effect=AgentError("socket missing"))
        self.rebuild(manager)
        self.assertEqual(len(server.store), 0)


class WatchAgentsTests(unittest.TestCase):
    """Agent death arrives as an event, not a probe."""

    def setUp(self) -> None:
        server.store = IssueStore()

    def watch(self, manager: FakeManager) -> None:
        drain_events(manager)

    def test_an_exited_agent_orphans_its_issue(self) -> None:
        server.store.add(
            Issue(
                key="JIM-88",
                title="",
                status=IssueStatus.IN_PROGRESS,
                agent=AgentRef("fg-jim-88", "abc"),
            )
        )
        manager = FakeManager()
        manager.stream = [
            AgentEvent(AgentEventKind.EXITED, AgentRef("fg-jim-88"), AgentStatus.GONE)
        ]
        self.watch(manager)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.ORPHANED)

    def test_status_changes_do_not_orphan_anything(self) -> None:
        server.store.add(
            Issue(key="JIM-88", title="", status=IssueStatus.IN_PROGRESS)
        )
        manager = FakeManager()
        manager.stream = [
            AgentEvent(
                AgentEventKind.STATUS_CHANGED,
                AgentRef("fg-jim-88"),
                AgentStatus.WORKING,
            )
        ]
        self.watch(manager)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_a_completed_issue_is_not_orphaned_by_its_own_teardown(self) -> None:
        # Foregent stops the agent itself once the issue completes (JIM-100),
        # and the harness reports that as the same EXITED event as a crash.
        # The issue's own status is what tells them apart.
        server.store.add(
            Issue(
                key="JIM-88",
                title="",
                status=IssueStatus.DONE,
                agent=AgentRef("fg-jim-88"),
            )
        )
        manager = FakeManager()
        manager.stream = [
            AgentEvent(AgentEventKind.EXITED, AgentRef("fg-jim-88"), AgentStatus.GONE)
        ]
        with self.assertNoLogs(server.logger, "WARNING"):
            self.watch(manager)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.DONE)

    def test_orphaning_is_a_no_op_for_issues_with_no_agent_to_lose(self) -> None:
        # Nothing to transition out of, so nothing is recorded and the
        # operator is not warned twice about one dead agent.
        server.store.add(Issue(key="JIM-88", title="", status=IssueStatus.ORPHANED))
        manager = FakeManager()
        manager.stream = [
            AgentEvent(AgentEventKind.EXITED, AgentRef("fg-jim-88"), AgentStatus.GONE),
            # An agent for an issue foregent is not tracking at all.
            AgentEvent(AgentEventKind.EXITED, AgentRef("fg-jim-99"), AgentStatus.GONE),
        ]
        with self.assertNoLogs(server.logger, "WARNING"):
            self.watch(manager)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.ORPHANED)
        self.assertIsNone(server.store.get("JIM-99"))

    def test_an_orphaned_issue_frees_capacity(self) -> None:
        # A dead agent must not go on holding the only slot.
        server.store.add(
            Issue(
                key="JIM-88",
                title="",
                status=IssueStatus.IN_PROGRESS,
                agent=AgentRef("fg-jim-88"),
            )
        )
        manager = FakeManager()
        manager.stream = [
            AgentEvent(AgentEventKind.EXITED, AgentRef("fg-jim-88"), AgentStatus.GONE)
        ]
        self.watch(manager)
        self.assertFalse(any(i.status in IN_FLIGHT for i in server.store))


@unittest.skipUnless(shutil.which("jj"), "jj is not installed")
class WorkspaceDispatchTests(unittest.IsolatedAsyncioTestCase):
    """Dispatch builds the agent a workspace, and completion removes it (JIM-59)."""

    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        self.enterContext(mock.patch.object(server.linear, "claim_issue"))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                    "FOREGENT_WORKSPACE_ROOT": str(self.tmp / "workspaces"),
                },
            )
        )
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        for args in (
            ("git", "init", "--colocate"),
            ("describe", "-m", "first"),
            ("new",),
            ("bookmark", "create", "main", "-r", "@-"),
        ):
            self.jj(self.repo, *args)

    def jj(self, cwd: Path, *args: str) -> None:
        """Run one jj command, for arranging and inspecting fixtures."""
        subprocess.run(["jj", "--no-pager", *args], cwd=cwd, check=True)

    def git_head(self) -> str:
        """The subject of the commit git's ``main`` points at.

        Git's view rather than jj's, because reaching it is the whole point:
        a bookmark moved inside a workspace is invisible to git until a
        mutating jj command runs at the colocated root.
        """
        done = subprocess.run(
            ["git", "log", "-1", "--format=%s", "main"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def test_dispatch_launches_in_a_workspace_not_in_the_repo(self) -> None:
        server.store.queue("JIM-88", str(self.repo))

        server.dispatch()

        cwd = Path(self.manager.launched[0].cwd)
        self.assertEqual(cwd, self.tmp / "workspaces" / "JIM-88")
        self.assertTrue(cwd.is_dir())
        # The store keeps both: the repo to forget the workspace, the path to
        # remove it.
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.repo, str(self.repo))
        self.assertEqual(issue.directory, str(cwd))

    def test_dispatch_briefs_the_mode_the_repos_remotes_name(self) -> None:
        # An `origin` on GitHub is somewhere a pull request can be opened, so
        # the agent is told to open one (JIM-152).
        subprocess.run(
            ["jj", "--no-pager", "git", "remote", "add", "origin",
             "https://github.com/jimpo/foregent"],
            cwd=self.repo,
            check=True,
        )
        server.store.queue("JIM-88", str(self.repo))

        server.dispatch()

        self.assertEqual(self.manager.sent[0][1], "/foregent-worker JIM-88 pull-request")

    def test_dispatch_briefs_bootstrap_for_a_repo_with_no_origin(self) -> None:
        server.store.queue("JIM-88", str(self.repo))

        server.dispatch()

        self.assertEqual(self.manager.sent[0][1], "/foregent-worker JIM-88 bootstrap")

    def test_a_workspace_that_cannot_be_built_fails_the_dispatch(self) -> None:
        # And leaves the issue Queued, so a retry picks it up rather than
        # stranding it as In Progress with no agent.
        server.store.queue("JIM-88", str(self.repo))

        with mock.patch.object(
            server.workspaces, "create", side_effect=WorkspaceError("no disk")
        ):
            with self.assertRaises(server.HTTPException) as caught:
                server.dispatch()

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("no disk", str(caught.exception.detail))
        self.assertEqual(self.manager.launched, [])
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.QUEUED)

    async def test_completion_removes_the_workspace(self) -> None:
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)

        await server.complete_task("JIM-88")

        self.assertFalse(cwd.exists())

    async def test_completion_removes_a_workspace_recovered_after_a_restart(
        self,
    ) -> None:
        # The bridge is restarted between dispatch and completion — the way an
        # operator picks up a merged change — so the issue foregent completes
        # is the one `rebuild_store` reconstructed from the live agent, not
        # the one dispatch wrote (JIM-150).
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        server.store = IssueStore()
        server.rebuild_store()

        await server.complete_task("JIM-88")

        self.assertFalse(cwd.exists())

    async def test_the_workspace_outlives_the_agent_that_used_it(self) -> None:
        # Removing a live agent's own cwd is worse than leaking a directory,
        # so teardown waits for the stop.
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        alive: list[bool] = []
        self.manager.on_stop = lambda: alive.append(cwd.is_dir())

        await server.complete_task("JIM-88")

        self.assertEqual(alive, [True])

    async def test_a_failed_teardown_is_reported_not_swallowed(self) -> None:
        # Nobody owns the leftovers, so this does not get the silent
        # best-effort treatment agent teardown gets.
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()

        with mock.patch.object(
            server.workspaces, "destroy", side_effect=WorkspaceError("busy")
        ):
            result = await server.complete_task("JIM-88")

        self.assertIn("busy", result)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.DONE)

    async def test_completion_advances_trunk_onto_the_agents_work(self) -> None:
        # Bootstrap mode has no pull request to carry the work out of the
        # workspace, so the bridge lands it (JIM-152). The repo has no origin,
        # which is what makes this bootstrap.
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        (cwd / "b.txt").write_text("the agent's work\n")
        self.jj(cwd, "commit", "-m", "the agent's work")

        await server.complete_task("JIM-88")

        self.assertEqual(self.git_head(), "the agent's work")

    async def test_trunk_is_advanced_before_the_next_issue_is_dispatched(self) -> None:
        # The next dispatch builds its workspace on `main`, so a bookmark
        # moved after it would leave the next agent on a trunk this issue
        # never reached — silently dropping this issue's work from its base.
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        (cwd / "b.txt").write_text("the agent's work\n")
        self.jj(cwd, "commit", "-m", "the agent's work")
        server.store.queue("JIM-89", str(self.repo))

        await server.complete_task("JIM-88")

        second = Path(self.manager.launched[1].cwd)
        self.assertTrue((second / "b.txt").is_file())

    async def test_completing_twice_is_safe(self) -> None:
        # Retrying a completion has always been safe, and landing must not
        # change that: the second call has no workspace left to name a
        # revision in, so jj would refuse for a reason about nothing.
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        (cwd / "b.txt").write_text("the agent's work\n")
        self.jj(cwd, "commit", "-m", "the agent's work")

        await server.complete_task("JIM-88")
        result = await server.complete_task("JIM-88")

        self.assertIn("complete", result)
        self.assertNotIn("not completed", result)
        self.assertEqual(self.git_head(), "the agent's work")

    async def test_a_pull_request_project_leaves_trunk_to_the_reviewer(self) -> None:
        self.jj(self.repo, "git", "remote", "add", "origin",
                "https://github.com/jimpo/foregent")
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        (cwd / "b.txt").write_text("pushed on a branch\n")
        self.jj(cwd, "commit", "-m", "pushed on a branch")

        await server.complete_task("JIM-88")

        self.assertEqual(self.git_head(), "first")

    async def test_work_that_never_rebased_stops_the_completion(self) -> None:
        # jj refuses to move `main` onto commits it is not an ancestor of, and
        # those commits exist only in the workspace — so tearing it down would
        # take them with it. The issue stays in flight and the workspace stays
        # on disk (JIM-152).
        server.store.queue("JIM-88", str(self.repo))
        server.dispatch()
        cwd = Path(self.manager.launched[0].cwd)
        (cwd / "b.txt").write_text("work off a stale trunk\n")
        self.jj(cwd, "commit", "-m", "work off a stale trunk")
        # Trunk moves on underneath the agent, the way another issue landing
        # would.
        (self.repo / "c.txt").write_text("landed elsewhere\n")
        self.jj(self.repo, "commit", "-m", "another issue landed")
        self.jj(self.repo, "bookmark", "set", "main", "-r", "@-")

        result = await server.complete_task("JIM-88")

        self.assertIn("not completed", result)
        self.assertTrue(cwd.is_dir())
        self.assertEqual(self.manager.stopped, [])
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)


class CompleteTaskTests(unittest.IsolatedAsyncioTestCase):
    """The agent-facing completion tool, teardown included."""

    async def test_completion_survives_the_teardown_it_triggers(self) -> None:
        # The whole loop in order (JIM-100): the tool marks the issue Done and
        # deliberately stops its agent, the harness reports that stop as an
        # exit like any other, and the consumer must leave the Done issue
        # alone rather than orphaning a completed one.
        server.store = IssueStore()
        ref = AgentRef("fg-jim-88", "conversation-1")
        server.store.add(
            Issue(key="JIM-88", title="", status=IssueStatus.IN_PROGRESS, agent=ref)
        )
        manager = FakeManager()
        with mock.patch.object(server, "manager", manager):
            await server.complete_task("JIM-88")
        self.assertEqual(manager.stopped, [ref])

        manager.stream = [AgentEvent(AgentEventKind.EXITED, ref, AgentStatus.GONE)]
        drain_events(manager)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.DONE)


if __name__ == "__main__":
    unittest.main()
