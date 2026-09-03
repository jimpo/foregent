"""Unit tests for the per-harness half of a launch: the kind and the argv.

One module per harness holds what its binary is called with
(:mod:`foregent.agents.claude`); :mod:`foregent.agents.harness` is what maps a
provider onto it. These pin both, so the manager's own tests can stay about
the socket calls.
"""

from __future__ import annotations

import json
import unittest

from foregent.agents import LaunchSpec, Provider
from foregent.agents.claude import render_args
from foregent.agents.harness import harness_for, provider_for_kind

_BASE = LaunchSpec(label="fg-jim-85", cwd="/ws/JIM-85")


def spec(**overrides) -> LaunchSpec:
    """A launch spec with only the fields a test cares about set."""
    from dataclasses import replace

    return replace(_BASE, **overrides)


class HarnessRegistryTests(unittest.TestCase):
    def test_every_provider_has_a_harness(self) -> None:
        # A provider the CLI accepts and nothing can start is a dispatch that
        # fails after the issue has already been claimed.
        for provider in Provider:
            self.assertEqual(harness_for(provider).provider, provider)

    def test_a_kind_names_the_provider_that_runs_it(self) -> None:
        # How a restart learns which harness a live agent runs, with nothing
        # persisted to read it from.
        for provider in Provider:
            kind = harness_for(provider).kind
            self.assertIs(provider_for_kind(kind), provider)

    def test_an_unknown_kind_names_nobody(self) -> None:
        # An operator's own agent of some other kind is not ours to attribute.
        self.assertIsNone(provider_for_kind("emacs"))


class ClaudeRenderArgsTests(unittest.TestCase):
    def test_a_fresh_agent_names_its_conversation(self) -> None:
        args = render_args(spec(conversation_id="abc-123"))
        self.assertIn("--session-id", args)
        self.assertEqual(args[args.index("--session-id") + 1], "abc-123")
        self.assertNotIn("--resume", args)

    def test_a_resumed_agent_continues_it_instead(self) -> None:
        # --session-id and --resume contradict each other: one names a new
        # conversation, the other reopens a recorded one.
        args = render_args(spec(conversation_id="abc-123", resume=True))
        self.assertIn("--resume", args)
        self.assertNotIn("--session-id", args)

    def test_permissions_are_always_bypassed(self) -> None:
        # Full permissions on a dedicated box: not a per-agent choice, so it
        # cannot be omitted by a caller.
        args = render_args(spec())
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")

    def test_mcp_servers_are_declared(self) -> None:
        args = render_args(spec(mcp_servers={"foregent": {"type": "http"}}))
        declared = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(declared, {"mcpServers": {"foregent": {"type": "http"}}})

    def test_declaring_servers_does_not_exclude_the_machines_own(self) -> None:
        # The two flags are independent: foregent can add its own tools
        # without also having to supply everything else the agent needs.
        args = render_args(spec(mcp_servers={"foregent": {"type": "http"}}))
        self.assertNotIn("--strict-mcp-config", args)

    def test_strict_mode_is_asked_for_explicitly(self) -> None:
        args = render_args(
            spec(mcp_servers={"foregent": {"type": "http"}}, strict_mcp=True)
        )
        self.assertIn("--strict-mcp-config", args)

    def test_no_mcp_servers_means_no_mcp_config(self) -> None:
        args = render_args(spec())
        self.assertNotIn("--mcp-config", args)
        self.assertNotIn("--strict-mcp-config", args)

    def test_optional_fields_are_omitted_when_unset(self) -> None:
        args = render_args(spec())
        for flag in ("--model", "--effort", "--append-system-prompt", "--allowedTools"):
            self.assertNotIn(flag, args)

    def test_label_becomes_the_display_name(self) -> None:
        args = render_args(spec())
        self.assertEqual(args[args.index("-n") + 1], "fg-jim-85")

    def test_the_binary_is_left_to_herdr(self) -> None:
        self.assertNotIn("claude", render_args(spec()))


if __name__ == "__main__":
    unittest.main()
