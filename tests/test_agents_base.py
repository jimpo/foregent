"""Tests for the AgentManager seam (JIM-84).

Types and a Protocol carry no behavior, so what is worth locking down is the
shape of the contract: a minimal manager satisfies it, and the pieces the
bridge persists survive a round trip.
"""

from __future__ import annotations

import unittest
from collections.abc import Collection, Iterator

from foregent.agents import (
    AgentEvent,
    AgentEventKind,
    AgentManager,
    AgentRecord,
    AgentRef,
    AgentStatus,
    LaunchSpec,
)


class StubManager:
    """The smallest thing that claims to be an AgentManager."""

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


if __name__ == "__main__":
    unittest.main()
