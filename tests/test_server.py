"""Tests for the bridge's dispatch path (JIM-88).

The server is driven through a fake :class:`~foregent.agents.AgentManager`,
which is the point of the seam: none of this knows what a herdr or a Claude
Code is. Linear is stubbed too — its own client is covered by
``tests.test_linear_integration``.
"""

from __future__ import annotations

import threading
import unittest
from collections.abc import Collection, Iterator
from unittest import mock

from foregent import server
from foregent.agents import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
)
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

    def launch(self, spec: LaunchSpec) -> AgentRef:
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
        # Agents still reach Linear through the machine's own configuration;
        # excluding it before declaring Linear explicitly (JIM-93) would cut
        # them off from the issue tracker entirely.
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
        server.store.block("JIM-88", "pr-review:foregent#1")
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
        with mock.patch.object(server, "manager", manager):
            server.watch_agents()
            # The consumer runs in a daemon thread; the fake stream is finite,
            # so it ends on its own.
            for thread in threading.enumerate():
                if thread.name == "foregent-agent-events":
                    thread.join(timeout=5)

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


if __name__ == "__main__":
    unittest.main()
