"""Tests for the bridge's dispatch path (JIM-88).

The server is driven through a fake :class:`~foregent.agents.AgentManager`,
which is the point of the seam: none of this knows what a herdr or a Claude
Code is. Linear is stubbed too — its own client is covered by
``tests.test_linear_integration``.
"""

from __future__ import annotations

import os
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
        if self.fail_send:
            raise self.fail_send
        self.sent.append((ref, text))

    def status(self, ref: AgentRef) -> AgentStatus:
        return AgentStatus.IDLE

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


class WakeTests(unittest.TestCase):
    """Prompting a parked agent with the event that resolved it (JIM-101)."""

    def setUp(self) -> None:
        server.store = IssueStore()
        self.manager = FakeManager()
        patcher = mock.patch.object(server, "manager", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_waking_sends_the_message_and_resumes_the_issue(self) -> None:
        self.park()
        record = server.wake_issue("JIM-88", "AJ commented: ship it")
        self.assertEqual(self.manager.sent, [(self.ref, "AJ commented: ship it")])
        self.assertEqual(record["status"], IssueStatus.IN_PROGRESS)
        self.assertEqual(record["blocker"], "")

    def test_waking_does_not_dispatch_anything_else(self) -> None:
        # The woken agent held its capacity slot the whole time it was parked
        # (docs/PLAN.md §5.6), so waking it frees nothing.
        self.park()
        server.store.queue("JIM-89", "/ws/JIM-89")
        server.wake_issue("JIM-88", "go on")
        self.assertEqual(self.manager.launched, [])

    def test_waking_an_issue_that_is_not_blocked_is_a_conflict(self) -> None:
        server.store.add(
            Issue(key="JIM-88", title="", status=IssueStatus.IN_PROGRESS, agent=self.ref)
        )
        with self.assertRaises(server.HTTPException) as caught:
            server.wake_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.manager.sent, [])

    def test_waking_an_untracked_issue_is_a_conflict(self) -> None:
        with self.assertRaises(server.HTTPException) as caught:
            server.wake_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.manager.sent, [])

    def test_waking_a_blocked_issue_with_no_agent_is_a_conflict(self) -> None:
        # `block()` upserts an unknown key, so an issue can carry a blocker
        # with nothing to prompt.
        server.store.block("JIM-88", "a review of the PR")
        with self.assertRaises(server.HTTPException) as caught:
            server.wake_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_harness_failure_leaves_the_issue_blocked(self) -> None:
        # So a retry is safe: nothing was delivered, and the blocker is still
        # recorded to match the next event against.
        self.park()
        self.manager.fail_send = AgentError("prompt never landed")
        with self.assertRaises(server.HTTPException) as caught:
            server.wake_issue("JIM-88", "go on")
        self.assertEqual(caught.exception.status_code, 502)
        issue = server.store.get("JIM-88")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.BLOCKED)
        self.assertEqual(issue.blocker, "a review of the PR")


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

    def tick(self, cursor: str = "T0", viewer: str = "") -> tuple[str, str]:
        return server.poll_tick(cursor, viewer)

    def test_a_comment_on_a_parked_issue_wakes_its_agent(self) -> None:
        self.park()
        self.answer([comment("JIM-36", body="ship it")], cursor="T1")
        self.tick()
        _, text = self.manager.sent[0]
        self.assertIn("ship it", text)
        issue = server.store.get("JIM-36")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

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

    def test_an_event_for_an_issue_nobody_is_parked_on_is_dropped(self) -> None:
        # Every in-flight issue is polled, so most events arrive for a working
        # agent. Delivering to those is a later ticket; dropping them is not
        # an error and must not stop the pass.
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
