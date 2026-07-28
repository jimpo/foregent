"""Tests for the AgentManager seam (JIM-84).

Types and a Protocol carry no behavior, so what is worth locking down is the
shape of the contract: a minimal manager satisfies it, and the pieces the
bridge persists survive a round trip.
"""

from __future__ import annotations

import unittest
from collections.abc import Collection, Iterator

from foregent.agents import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
    issue_key_from_label,
    label_for,
)


class StubManager:
    """The smallest thing that claims to be an AgentManager."""

    def describe(self) -> str:
        return "a stub harness"

    def launch(self, spec: LaunchSpec) -> AgentRef:
        return AgentRef(spec.label, spec.conversation_id)

    def send(self, ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
        return None

    def status(self, ref: AgentRef) -> AgentStatus:
        return AgentStatus.IDLE

    def wait(
        self,
        ref: AgentRef,
        until: Collection[AgentStatus],
        timeout: float,
    ) -> AgentStatus:
        return AgentStatus.IDLE

    def read(self, ref: AgentRef, lines: int = 50) -> str:
        return ""

    def stop(self, ref: AgentRef) -> None:
        return None

    def list_agents(self) -> list[AgentRecord]:
        return []

    def events(self) -> Iterator[AgentEvent]:
        return iter(())


class ProtocolTests(unittest.TestCase):
    def test_a_minimal_manager_satisfies_the_protocol(self) -> None:
        self.assertIsInstance(StubManager(), AgentManager)

    def test_a_manager_missing_a_method_does_not(self) -> None:
        class Partial:
            def launch(self, spec: LaunchSpec) -> AgentRef:
                return AgentRef(spec.label)

        self.assertNotIsInstance(Partial(), AgentManager)


class TypeTests(unittest.TestCase):
    def test_gone_is_distinct_from_unknown(self) -> None:
        # UNKNOWN means "running, state unreadable"; GONE means the process
        # is over. Collapsing them is how a harness loses crash authority.
        self.assertNotEqual(AgentStatus.GONE, AgentStatus.UNKNOWN)

    def test_launch_spec_defaults_leave_the_harness_in_charge(self) -> None:
        spec = LaunchSpec(label="fg-jim-84", cwd="/ws/JIM-84")
        self.assertIsNone(spec.model)
        self.assertIsNone(spec.conversation_id)
        self.assertFalse(spec.resume)
        self.assertEqual(spec.env, {})

    def test_ref_carries_the_durable_conversation_id(self) -> None:
        # The label locates the live process; the conversation id is what
        # outlives it and gets recorded in Linear (docs/PLAN.md §5.11).
        ref = AgentRef("fg-jim-84", "11111111-2222-3333-4444-555555555555")
        self.assertEqual(ref.conversation_id, "11111111-2222-3333-4444-555555555555")

    def test_exit_events_report_gone(self) -> None:
        event = AgentEvent(
            AgentEventKind.EXITED, AgentRef("fg-jim-84"), AgentStatus.GONE
        )
        self.assertEqual(event.kind, AgentEventKind.EXITED)
        self.assertEqual(event.status, AgentStatus.GONE)


class LabelTests(unittest.TestCase):
    """The naming convention the bridge rebuilds its state from (§5.11)."""

    def test_label_is_derived_from_the_issue_key(self) -> None:
        self.assertEqual(label_for("JIM-52"), "fg-jim-52")

    def test_labels_round_trip_back_to_issue_keys(self) -> None:
        self.assertEqual(issue_key_from_label(label_for("JIM-52")), "JIM-52")

    def test_labels_are_deterministic(self) -> None:
        # A retry after a half-finished launch must ask for the name that is
        # already taken, so the harness refuses it instead of running a
        # second agent on one issue.
        self.assertEqual(label_for("JIM-52"), label_for("JIM-52"))

    def test_foreign_labels_are_not_ours(self) -> None:
        self.assertIsNone(issue_key_from_label("scratch"))
        self.assertIsNone(issue_key_from_label("fg-"))

    def test_an_unusable_key_is_rejected_at_the_source(self) -> None:
        # Better to fail where the key is known than to have the harness
        # reject an opaque name mid-dispatch.
        for key in ["JIM 52", "JIM/52", "JIM.52", "J" * 40]:
            with self.assertRaises(AgentError):
                label_for(key)


if __name__ == "__main__":
    unittest.main()
