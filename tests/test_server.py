"""Tests for the bridge's dispatch path (JIM-88).

The server is driven through a fake :class:`~foregent.agents.AgentManager`,
which is the point of the seam: none of this knows what a herdr or a Claude
Code is. Linear is stubbed too — its own client is covered by
``tests.test_linear_integration``.
"""

from __future__ import annotations

import os
import queue
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
from foregent.events import Event, EventKind
from foregent.models import Issue, IssueStatus
from foregent.store import IssueStore


class FakeManager:
    """An agent harness that records what the bridge asked it to do."""

    def __init__(self, existing: list[AgentRecord] | None = None) -> None:
        self.launched: list[LaunchSpec] = []
        self.sent: list[tuple[AgentRef, str]] = []
        self.stopped: list[AgentRef] = []
        self.existing = existing or []
        self.stream: list[AgentEvent] = []
        self.fail_launch: Exception | None = None
        self.fail_send: Exception | None = None
        self.fail_status: Exception | None = None
        # How many sends fail before one lands, negative for all of them.
        # The delivery drainer keeps offering a message to a busy agent, so
        # a harness that recovers and one that never does are both worth
        # driving (JIM-132).
        self.fail_sends = -1
        self.agent_status = AgentStatus.IDLE
        # Observes the world as the agent starts, for the things dispatch has
        # to have finished by then rather than merely around then.
        self.at_launch: Callable[[], None] | None = None

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
        self.stopped.append(ref)

    def list_agents(self) -> list[AgentRecord]:
        return list(self.existing)

    def events(self) -> Iterator[AgentEvent]:
        return iter(self.stream)


def comment(key: str, actor: str = "operator", body: str = "") -> Event:
    """A comment event of the shape the Linear poll builds."""
    return Event(
        kind=EventKind.COMMENT, issue_key=key, actor=actor, author="AJ", body=body
    )


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
    """Run the delivery drainer until everything queued has been handled.

    Bounded, because the drainer is endless by design: a delivery that never
    finishes fails the test instead of hanging it.
    """
    server.watch_deliveries()
    waiter = threading.Thread(target=server.deliveries.join, daemon=True)
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
        # Nothing runs without a durable ownership record in Linear
        # (docs/PLAN.md §5.12).
        self.queue()
        server.dispatch()
        self.claim.assert_called_once_with("JIM-88")
        self.assertEqual(len(self.manager.launched), 1)

    def test_dispatch_launches_in_the_issues_workspace(self) -> None:
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
        issue = server.store.get("JIM-88")
        assert issue is not None and issue.agent is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)
        self.assertEqual(issue.agent, ref)
        # The conversation id is the half that outlives the process (§5.11).
        self.assertEqual(issue.agent.conversation_id, "conversation-1")

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
        # A blocked agent is alive in its workspace (docs/PLAN.md §5.6), so
        # it must keep occupying capacity.
        self.queue()
        server.dispatch()
        server.store.block("JIM-88", "a review of the PR")
        self.queue("JIM-89", "/ws/JIM-89")
        server.dispatch()
        self.assertEqual(len(self.manager.launched), 1)

    def test_completing_an_issue_dispatches_the_next(self) -> None:
        self.queue()
        server.dispatch()
        self.queue("JIM-89", "/ws/JIM-89")
        server.complete_issue("JIM-88")
        self.assertEqual(
            [spec.label for spec in self.manager.launched], ["fg-jim-88", "fg-jim-89"]
        )


class DeliverTests(unittest.TestCase):
    """Queueing an event for the agent it was for (JIM-131, JIM-132)."""

    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        # A queue per test, so a drainer left over from an earlier one cannot
        # take this test's messages.
        self.enterContext(mock.patch.object(server, "deliveries", queue.Queue()))
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
        # A send waits out whatever the agent is doing, and Linear retries
        # any webhook delivery the bridge is slow to answer, so no ingesting
        # caller may be held behind an agent mid-turn (JIM-132).
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

    def test_a_busy_agent_is_waited_out_rather_than_given_up_on(self) -> None:
        # `send` fails once the harness's own idle budget runs out, and that
        # says the agent is still working, not that the message is lost: five
        # minutes is not a reason to throw an event away (JIM-132).
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

    def test_delivering_does_not_dispatch_anything_else(self) -> None:
        # The agent held its capacity slot the whole time, parked or not
        # (docs/PLAN.md §5.6), so prompting it frees nothing.
        self.park()
        server.store.queue("JIM-89", "/ws/JIM-89")
        server.deliver_issue("JIM-88", "go on")
        drain_deliveries()
        self.assertEqual(self.manager.launched, [])

    def test_delivering_to_an_untracked_issue_is_a_conflict(self) -> None:
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(server.deliveries.empty())

    def test_delivering_to_a_queued_issue_is_a_conflict(self) -> None:
        # Nothing is running yet: the brief at dispatch is what it will read.
        server.store.queue("JIM-88", "/ws/JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(server.deliveries.empty())

    def test_delivering_to_an_orphaned_issue_is_a_conflict(self) -> None:
        self.work()
        server.store.orphan("JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(server.deliveries.empty())

    def test_delivering_to_a_completed_issue_is_a_conflict(self) -> None:
        # Done keeps the ref of the agent foregent has since stopped, so the
        # status is what rules it out, not the missing agent.
        self.work()
        server.store.complete("JIM-88")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(server.deliveries.empty())

    def test_delivering_to_a_blocked_issue_with_no_agent_is_a_conflict(self) -> None:
        # `block()` upserts an unknown key, so an issue can carry a blocker
        # with nothing to prompt.
        server.store.block("JIM-88", "a review of the PR")
        with self.assertRaises(server.HTTPException) as caught:
            server.deliver_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(server.deliveries.empty())


class CheckHerdrProtocolTests(unittest.TestCase):
    """Refusing to start on a herdr protocol drift (docs/PLAN.md §5.8)."""

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
    """Recovering the issue<->agent map from the harness (docs/PLAN.md §5.11)."""

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
    """Agent death arrives as an event, not a probe (docs/PLAN.md §5.6)."""

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
        occupied = (IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED)
        self.assertFalse(any(i.status in occupied for i in server.store))


class PollTickTests(unittest.TestCase):
    """The tick that feeds the matcher (JIM-36, docs/PLAN.md §5.1).

    Linear is stubbed: the live query shape is covered by
    ``tests.test_linear_integration``, and what matters here is what the
    bridge does with what comes back.
    """

    # Foregent's own Linear account, which it writes as on every claim and
    # every comment an agent posts through the Linear MCP.
    VIEWER = "viewer-id"

    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        self.enterContext(mock.patch.object(server, "deliveries", queue.Queue()))
        self.viewer = self.enterContext(
            mock.patch.object(server.linear, "viewer_id", return_value=self.VIEWER)
        )
        self.poll = self.enterContext(
            mock.patch.object(server.linear, "poll_comments")
        )
        self.answer([])

    def answer(self, events: list[Event], cursor: str = "T0") -> None:
        """Have the next poll return ``events`` and hand back ``cursor``."""
        self.poll.return_value = (events, cursor)

    def park(self, key: str = "JIM-36") -> None:
        server.store.add(
            Issue(
                key=key,
                title="",
                status=IssueStatus.BLOCKED,
                blocker="a review",
                agent=AgentRef(f"fg-{key.lower()}", "conversation-1"),
            )
        )

    def work(self, key: str = "JIM-36") -> None:
        server.store.add(
            Issue(
                key=key,
                title="",
                status=IssueStatus.IN_PROGRESS,
                agent=AgentRef(f"fg-{key.lower()}", "conversation-1"),
            )
        )

    def tick(self, cursor: str = "T0", viewer: str = "") -> tuple[str, str]:
        """One pass, plus the drainer that hands what it found to the agents."""
        result = server.poll_tick(cursor, viewer)
        drain_deliveries()
        return result

    def test_a_comment_on_a_parked_issue_wakes_its_agent(self) -> None:
        self.park()
        self.answer([comment("JIM-36", body="ship it")], cursor="T1")
        self.tick()
        _, text = self.manager.sent[0]
        self.assertIn("Waking", text)
        self.assertIn("ship it", text)
        issue = server.store.get("JIM-36")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_a_comment_on_a_working_issue_reaches_its_agent_too(self) -> None:
        # JIM-131: a worker sees activity on its own issue as it happens, and
        # is not told it is being woken from a block it never reported.
        self.work()
        self.answer([comment("JIM-36", body="ship it")], cursor="T1")
        self.tick()
        _, text = self.manager.sent[0]
        self.assertNotIn("Waking", text)
        self.assertIn("ship it", text)

    def test_a_delivery_to_a_working_agent_leaves_its_issue_alone(self) -> None:
        # Nothing about it was waiting: not its status, and not its slot.
        self.work()
        server.store.queue("JIM-41", "/ws/JIM-41")
        self.answer([comment("JIM-36")], cursor="T1")
        self.tick()
        issue = server.store.get("JIM-36")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)
        self.assertEqual(self.manager.launched, [])

    def test_the_cursor_advances_only_over_what_was_served(self) -> None:
        # Cursor, not clock (JIM-36): the next window starts at the last
        # comment actually seen, so a slow or restarted tick cannot skip one.
        self.park()
        self.answer([comment("JIM-36")], cursor="T1")
        cursor, _ = self.tick("T0")
        self.assertEqual(cursor, "T1")

    def test_a_quiet_window_leaves_the_cursor_where_it_was(self) -> None:
        self.park()
        self.answer([], cursor="T0")
        cursor, _ = self.tick("T0")
        self.assertEqual(cursor, "T0")
        self.assertEqual(self.manager.sent, [])

    def test_only_in_flight_issues_are_polled(self) -> None:
        # Cost scales with work in progress, not with workspace size.
        self.park("JIM-36")
        server.store.add(Issue(key="JIM-40", title="", status=IssueStatus.DONE))
        server.store.queue("JIM-41", "/ws/JIM-41")
        self.tick()
        self.assertEqual(self.poll.call_args.args[0], ["JIM-36"])

    def test_foregents_own_comment_wakes_nobody(self) -> None:
        # A wake that causes a write is a loop. The query drops these
        # server-side too; this is the second half of the same guard.
        self.park()
        self.answer([comment("JIM-36", actor=self.VIEWER)])
        self.tick()
        self.assertEqual(self.manager.sent, [])
        issue = server.store.get("JIM-36")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.BLOCKED)

    def test_an_event_for_an_issue_with_no_agent_is_dropped(self) -> None:
        # The normal case, not an error: an in-flight issue can be recorded
        # with nothing behind it, and a person comments on issues foregent is
        # not tracking at all. Neither may stop the pass.
        server.store.add(
            Issue(key="JIM-36", title="", status=IssueStatus.IN_PROGRESS)
        )
        self.answer([comment("JIM-36"), comment("JIM-99")], cursor="T1")
        cursor, _ = self.tick("T0")
        self.assertEqual(self.manager.sent, [])
        self.assertEqual(cursor, "T1")

    def test_a_linear_outage_is_survived_and_retried(self) -> None:
        # Lateness is polling's failure mode, and it self-heals: the cursor
        # does not move, so the next tick asks for the same window.
        self.park()
        self.poll.side_effect = server.linear.LinearError("502 from Linear")
        cursor, _ = self.tick("T0")
        self.assertEqual(cursor, "T0")

    def test_nothing_is_polled_until_the_viewer_is_known(self) -> None:
        # Without it foregent cannot tell its own writes apart, so it must not
        # poll at all rather than poll and wake agents with themselves.
        self.park()
        self.viewer.side_effect = server.linear.LinearError("no API key")
        cursor, viewer = self.tick("T0")
        self.assertEqual(self.poll.call_count, 0)
        self.assertEqual((cursor, viewer), ("T0", ""))

    def test_the_viewer_is_resolved_once_and_carried(self) -> None:
        self.park()
        _, viewer = self.tick()
        self.assertEqual(viewer, self.VIEWER)
        self.tick(viewer=viewer)
        self.viewer.assert_called_once()
        self.assertEqual(self.poll.call_args.args[2], self.VIEWER)


class CompleteTaskTests(unittest.IsolatedAsyncioTestCase):
    """The agent-facing completion tool, teardown included (docs/PLAN.md §5.6)."""

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
