"""Unit tests for the per-harness half of a launch: the kind and the argv.

One module per harness holds what its binary is called with
(:mod:`foregent.agents.claude`); :mod:`foregent.agents.harness` is what maps a
provider onto it. These pin both, so the manager's own tests can stay about
the socket calls.
"""

from __future__ import annotations

import json
import unittest

from foregent.agents import LaunchSpec, McpServer, Provider
from foregent.agents import codex as codex_harness
from foregent.agents.base import AgentError
from foregent.agents.claude import render_args
from foregent.agents.harness import harness_for, provider_for_kind

_BASE = LaunchSpec(label="fg-jim-85", cwd="/ws/JIM-85")
URL = "http://127.0.0.1:8577/mcp"


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
        args = render_args(spec(mcp_servers={"foregent": McpServer(url=URL)}))
        declared = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(
            declared, {"mcpServers": {"foregent": {"type": "http", "url": URL}}}
        )

    def test_a_token_is_named_and_never_written(self) -> None:
        # Claude Code expands the variable per session, so the config this
        # lands in never holds the credential itself.
        args = render_args(
            spec(mcp_servers={"linear": McpServer(URL, token_env="LINEAR_API_KEY")})
        )
        declared = json.loads(args[args.index("--mcp-config") + 1])
        headers = declared["mcpServers"]["linear"]["headers"]
        self.assertEqual(headers, {"Authorization": "Bearer ${LINEAR_API_KEY}"})

    def test_declaring_servers_does_not_exclude_the_machines_own(self) -> None:
        # The two flags are independent: foregent can add its own tools
        # without also having to supply everything else the agent needs.
        args = render_args(spec(mcp_servers={"foregent": McpServer(url=URL)}))
        self.assertNotIn("--strict-mcp-config", args)

    def test_strict_mode_is_asked_for_explicitly(self) -> None:
        args = render_args(
            spec(mcp_servers={"foregent": McpServer(url=URL)}, strict_mcp=True)
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

    def test_the_brief_invokes_the_skill(self) -> None:
        # A Claude Code slash command, so the lifecycle stays in the skill.
        brief = harness_for(Provider.CLAUDE).brief("JIM-42", "pull-request")
        self.assertEqual(brief, "/foregent-worker JIM-42 pull-request")


class CodexRenderArgsTests(unittest.TestCase):
    def render(self, **overrides) -> list[str]:
        return codex_harness.render_args(spec(**overrides))

    def test_permissions_are_always_bypassed(self) -> None:
        # Full permissions on a dedicated box: not a per-agent choice, so it
        # cannot be omitted by a caller.
        self.assertIn(codex_harness.BYPASS, self.render())

    def test_a_fresh_agent_cannot_be_told_its_conversation(self) -> None:
        # Codex has no counterpart of `--session-id`; it records a session of
        # its own, which herdr reports back after the agent has started.
        args = self.render(conversation_id="abc-123")
        self.assertNotIn("abc-123", args)
        self.assertNotIn("resume", args)

    def test_a_resumed_agent_continues_the_recorded_session(self) -> None:
        args = self.render(conversation_id="abc-123", resume=True)
        self.assertEqual(args[:2], ["resume", "abc-123"])

    def test_resuming_without_a_session_is_refused(self) -> None:
        # Codex would open its session picker and wait, with nobody there.
        with self.assertRaises(AgentError):
            self.render(resume=True)

    def test_effort_is_a_config_override(self) -> None:
        args = self.render(effort="high")
        self.assertEqual(args[args.index("-c") + 1], "model_reasoning_effort=high")

    def test_mcp_servers_are_declared_a_leaf_at_a_time(self) -> None:
        # `-c` parses TOML and falls back to a literal string, so a URL needs
        # no quoting and foregent writes no TOML of its own.
        args = self.render(mcp_servers={"foregent": McpServer(url=URL)})
        self.assertIn(f"mcp_servers.foregent.url={URL}", args)

    def test_a_token_is_named_and_never_written(self) -> None:
        args = self.render(
            mcp_servers={"linear": McpServer(URL, token_env="LINEAR_API_KEY")}
        )
        self.assertIn(
            "mcp_servers.linear.bearer_token_env_var=LINEAR_API_KEY", args
        )

    def test_what_codex_cannot_express_is_refused(self) -> None:
        # Silently dropping a restriction the caller asked for is the failure
        # nobody notices.
        for overrides in (
            {"system_prompt": "be brief"},
            {"tools_allow": ("Bash",)},
            {"tools_deny": ("Bash",)},
            {"strict_mcp": True},
        ):
            with self.subTest(**overrides), self.assertRaises(AgentError):
                self.render(**overrides)

    def test_the_binary_is_left_to_herdr(self) -> None:
        self.assertNotIn("codex", self.render())

    def test_the_brief_names_the_skill(self) -> None:
        # Codex has no slash form for a skill; it lists what it found by name,
        # so naming it is what makes an agent read it.
        brief = harness_for(Provider.CODEX).brief("JIM-42", "pull-request")
        self.assertIn("foregent-worker", brief)
        self.assertIn("JIM-42", brief)
        self.assertIn("pull-request", brief)


if __name__ == "__main__":
    unittest.main()
