"""Unit tests for the herdr + Claude Code manager (JIM-85).

Driven through a fake herdr client: these pin the socket calls the manager
makes and the flags it renders, without needing a server. The live behavior
is covered by ``ManagerIntegrationTests`` in ``tests.test_herdr_integration``.
"""

from __future__ import annotations

import json
import os
import unittest
from itertools import islice
from collections.abc import Callable, Iterator
from dataclasses import replace
from unittest import mock

from foregent import herdr
from foregent.agents import (
    AgentError,
    AgentEventKind,
    AgentRef,
    AgentStatus,
    LaunchSpec,
)
from foregent.agents.herdr_claude import HerdrClaudeManager, render_args

WORKSPACE = {
    "workspace": {"workspace_id": "w1", "label": "JIM-85"},
    "root_pane": {"pane_id": "w1:p1"},
}

# A live, prompt-ready agent as herdr reports it. `interactive_ready` is the
# real precondition for prompting: an agent reads as idle several seconds
# before its TUI will accept input.
READY_AGENT = {
    "agent": {
        "agent_status": "idle",
        "interactive_ready": True,
        "state_change_seq": 1,
        "workspace_id": "w1",
        "pane_id": "w1:p1",
    }
}


_BASE = LaunchSpec(label="fg-jim-85", cwd="/ws/JIM-85")


def status_changed(pane_id: str, status: str, event: str = "pane.agent_status_changed") -> dict:
    """A herdr status-change envelope, as it arrives on the wire.

    herdr sends this one dotted, unlike the underscored names it uses for
    every other event.
    """
    return {
        "event": event,
        "data": {"pane_id": pane_id, "agent": "claude", "agent_status": status},
    }


def spec(**overrides) -> LaunchSpec:
    """A launch spec with the fields every test needs already set."""
    return replace(_BASE, **overrides)


# A canned answer: a fixed payload, or one computed from the request when a
# test needs the server to change between calls.
Answer = dict | Callable[[dict], dict]

# What herdr answers when a test does not care about the details.
_DEFAULTS: dict[str, Answer] = {
    "agent.get": READY_AGENT,
    "agent.read": {"read": {"text": ""}},
}


class FakeClient:
    """Records calls and answers from a canned table."""

    def __init__(
        self,
        responses: dict[str, Answer] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, Answer] = responses or {}
        self.errors = errors or {}
        # A canned event stream; a None entry stands for a quiet tick.
        self.stream: list[dict | None] = []

    def describe(self) -> str:
        return "a fake herdr"

    def call(self, method: str, params: dict | None = None, **kwargs) -> dict:
        self.calls.append((method, params or {}))
        if method in self.errors:
            raise self.errors[method]
        answer: Answer = self.responses.get(method, _DEFAULTS.get(method, {}))
        return answer if isinstance(answer, dict) else answer(params or {})

    def subscribe(
        self, subscriptions: list[dict], tick: float | None = None
    ) -> Iterator[dict | None]:
        """Replay canned event envelopes, then end the stream."""
        self.calls.append(("events.subscribe", {"subscriptions": subscriptions}))
        yield from self.stream

    def params_for(self, method: str) -> dict:
        """Params of the first call to ``method``."""
        return next(params for name, params in self.calls if name == method)

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def count(self, method: str) -> int:
        return self.methods().count(method)


def manager(client: FakeClient) -> HerdrClaudeManager:
    return HerdrClaudeManager(client)  # ty: ignore[invalid-argument-type]


class SessionTests(unittest.TestCase):
    """Which herdr the manager talks to, given what it was constructed with."""

    def test_a_named_session_gets_that_sessions_socket(self) -> None:
        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": "/tmp/other.sock"}, clear=True
        ):
            client = HerdrClaudeManager(session="foregent").client
        self.assertEqual(
            client.path,
            str(herdr.CONFIG_DIR / "sessions" / "foregent" / "herdr.sock"),
        )

    def test_no_session_means_the_one_this_process_runs_in(self) -> None:
        # herdr injects HERDR_SOCKET_PATH into every pane it owns, so a bridge
        # started from a pane finds its own session without being told.
        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": "/tmp/inherited.sock"}, clear=True
        ):
            runner = HerdrClaudeManager()
            self.assertEqual(runner.client.path, "/tmp/inherited.sock")
            self.assertIn("this process runs in", runner.describe())

    def test_nothing_at_all_falls_back_to_the_default_session(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            runner = HerdrClaudeManager()
            self.assertEqual(runner.client.path, str(herdr.CONFIG_DIR / "herdr.sock"))
            self.assertIn("default", runner.describe())

    def test_the_description_comes_from_the_client(self) -> None:
        # The client resolved the socket, so it is the only thing that can say
        # which session that was.
        self.assertEqual(manager(FakeClient()).describe(), "a fake herdr")


class RenderArgsTests(unittest.TestCase):
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


class LaunchTests(unittest.TestCase):
    def test_launch_opens_a_workspace_and_starts_the_agent(self) -> None:
        client = FakeClient({"workspace.create": WORKSPACE})
        ref = manager(client).launch(spec(conversation_id="abc-123"))

        self.assertEqual(
            client.methods()[:3],
            ["workspace.create", "agent.start", "agent.wait"],
        )
        created = client.params_for("workspace.create")
        self.assertEqual(created["cwd"], "/ws/JIM-85")
        # The workspace is labeled with the issue key, not the agent label:
        # it is what an attached operator scans the session for.
        self.assertEqual(created["label"], "JIM-85")

        started = client.params_for("agent.start")
        self.assertEqual(started["name"], "fg-jim-85")
        self.assertEqual(started["kind"], "claude")
        self.assertEqual(started["pane_id"], "w1:p1")
        self.assertEqual(ref, AgentRef("fg-jim-85", "abc-123"))

    def test_launch_waits_for_idle_before_returning(self) -> None:
        client = FakeClient({"workspace.create": WORKSPACE})
        manager(client).launch(spec())
        self.assertEqual(client.params_for("agent.wait")["until"], ["idle"])

    def test_launch_assigns_a_conversation_id_when_given_none(self) -> None:
        # Every agent must be resumable from the moment it exists, so the id
        # cannot wait for the agent to report one.
        client = FakeClient({"workspace.create": WORKSPACE})
        ref = manager(client).launch(spec())
        self.assertTrue(ref.conversation_id)
        args = client.params_for("agent.start")["args"]
        self.assertEqual(args[args.index("--session-id") + 1], ref.conversation_id)

    def test_a_failed_start_does_not_leak_its_workspace(self) -> None:
        client = FakeClient(
            {"workspace.create": WORKSPACE},
            {"agent.start": herdr.HerdrAPIError("agent_exists", "taken")},
        )
        with self.assertRaises(AgentError):
            manager(client).launch(spec())
        self.assertEqual(client.params_for("workspace.close")["workspace_id"], "w1")

    def test_an_agent_that_never_settles_reports_the_screen(self) -> None:
        # A start that hangs is nearly always a modal (the trust dialog), so
        # the error carries the screen rather than just "timeout".
        client = FakeClient(
            {
                "workspace.create": WORKSPACE,
                "agent.read": {"read": {"text": "1. Yes, I trust this folder"}},
            },
            {"agent.wait": herdr.HerdrAPIError("timeout", "timed out")},
        )
        with self.assertRaises(AgentError) as caught:
            manager(client).launch(spec())
        self.assertIn("I trust this folder", str(caught.exception))
        self.assertEqual(client.params_for("workspace.close")["workspace_id"], "w1")


class SendTests(unittest.TestCase):
    """Delivery: never silently dropped, never silently doubled."""

    def setUp(self) -> None:
        for name, value in [("RETRY_SECONDS", 0), ("POLL_SECONDS", 0)]:
            patcher = mock.patch(f"foregent.agents.herdr_claude.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stalls(self, times: int) -> Answer:
        """A prompt herdr reports as stalled ``times`` times, then accepts."""
        attempts = {"count": 0}

        def prompt(params: dict) -> dict:
            attempts["count"] += 1
            if attempts["count"] <= times:
                raise herdr.HerdrAPIError(
                    "agent_prompt_stalled", "no observed state change"
                )
            return {"type": "agent_prompted"}

        return prompt

    def test_send_waits_for_a_free_agent_first(self) -> None:
        client = FakeClient({"agent.wait": {"agent": {"agent_status": "idle"}}})
        manager(client).send(AgentRef("fg-jim-86"), "go")
        self.assertEqual(client.params_for("agent.wait")["until"], ["done", "idle"])
        self.assertEqual(client.params_for("agent.prompt")["text"], "go")

    def test_every_prompt_asks_herdr_to_check_delivery(self) -> None:
        # Without a `wait` block herdr reports success even when the text is
        # swallowed by a modal, so the check is not optional.
        client = FakeClient()
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        wait = client.params_for("agent.prompt")["wait"]
        self.assertEqual(wait["until"], ["working"])
        # The budget has to outlast herdr's own five-second stall window.
        self.assertGreater(wait["timeout_ms"], 5_000)

    def test_send_can_skip_the_wait(self) -> None:
        client = FakeClient()
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertNotIn("agent.wait", client.methods())
        self.assertEqual(client.count("agent.prompt"), 1)

    def test_sending_to_a_dead_agent_is_an_error(self) -> None:
        client = FakeClient(
            errors={"agent.wait": herdr.HerdrAPIError("agent_not_found", "nope")}
        )
        with self.assertRaises(AgentError):
            manager(client).send(AgentRef("fg-jim-86"), "go")

    def test_a_stalled_prompt_is_resent(self) -> None:
        # A stall means the agent never saw it, so resending cannot double
        # up — and not resending would lose the message.
        client = FakeClient({"agent.prompt": self.stalls(1)})
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertEqual(client.count("agent.prompt"), 2)

    def test_an_undeliverable_prompt_fails_loudly(self) -> None:
        client = FakeClient(
            {"agent.read": {"read": {"text": "1. Yes, I trust this folder"}},
             "agent.prompt": self.stalls(99)}
        )
        with self.assertRaises(AgentError) as caught:
            manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertEqual(client.count("agent.prompt"), 3)
        # The screen goes in the error: the cause is nearly always visible.
        self.assertIn("I trust this folder", str(caught.exception))

    def test_a_reacting_agent_that_never_worked_still_counts(self) -> None:
        # herdr's stall check passed and only the `until` state was missed,
        # which is delivery, not failure.
        client = FakeClient(
            errors={"agent.prompt": herdr.HerdrAPIError("timeout", "timed out")}
        )
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertEqual(client.count("agent.prompt"), 1)

    def test_an_agent_not_ready_yet_is_retried(self) -> None:
        attempts = {"count": 0}

        def prompt(params: dict) -> dict:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise herdr.HerdrAPIError("agent_not_ready", "not active")
            return {"type": "agent_prompted"}

        client = FakeClient({"agent.prompt": prompt})
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertEqual(client.count("agent.prompt"), 2)

    def test_other_prompt_failures_are_not_retried(self) -> None:
        client = FakeClient(
            errors={"agent.prompt": herdr.HerdrAPIError("invalid_request", "bad")}
        )
        with self.assertRaises(AgentError):
            manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertEqual(client.count("agent.prompt"), 1)

    def test_send_waits_for_the_tui_to_accept_input(self) -> None:
        # An agent reads as idle before it will take a prompt; prompting in
        # that window is refused with `agent_not_ready`.
        polls = {"count": 0}

        def get(params: dict) -> dict:
            polls["count"] += 1
            return {"agent": {"interactive_ready": polls["count"] > 2}}

        client = FakeClient({"agent.get": get})
        manager(client).send(AgentRef("fg-jim-86"), "go", when_idle=False)
        self.assertGreater(polls["count"], 2)
        self.assertEqual(client.count("agent.prompt"), 1)


class StatusTests(unittest.TestCase):
    def test_status_maps_herdr_states(self) -> None:
        for herdr_status, expected in [
            ("idle", AgentStatus.IDLE),
            ("working", AgentStatus.WORKING),
            ("blocked", AgentStatus.BLOCKED),
            ("done", AgentStatus.DONE),
        ]:
            client = FakeClient({"agent.get": {"agent": {"agent_status": herdr_status}}})
            self.assertEqual(manager(client).status(AgentRef("fg-jim-85")), expected)

    def test_an_unrecognized_state_is_unknown_not_an_error(self) -> None:
        # A herdr release that adds a status must not crash the bridge.
        client = FakeClient({"agent.get": {"agent": {"agent_status": "meditating"}}})
        self.assertEqual(
            manager(client).status(AgentRef("fg-jim-85")), AgentStatus.UNKNOWN
        )

    def test_a_missing_agent_is_gone(self) -> None:
        client = FakeClient(
            errors={"agent.get": herdr.HerdrAPIError("agent_not_found", "nope")}
        )
        self.assertEqual(
            manager(client).status(AgentRef("fg-jim-85")), AgentStatus.GONE
        )

    def test_other_failures_are_not_mistaken_for_death(self) -> None:
        client = FakeClient(errors={"agent.get": herdr.HerdrTransportError("down")})
        with self.assertRaises(AgentError):
            manager(client).status(AgentRef("fg-jim-85"))


class WaitTests(unittest.TestCase):
    def test_wait_passes_the_requested_states(self) -> None:
        client = FakeClient({"agent.wait": {"agent": {"agent_status": "idle"}}})
        status = manager(client).wait(AgentRef("fg-jim-85"), {AgentStatus.IDLE}, 30)
        self.assertEqual(status, AgentStatus.IDLE)
        self.assertEqual(client.params_for("agent.wait")["until"], ["idle"])
        self.assertEqual(client.params_for("agent.wait")["timeout_ms"], 30_000)

    def test_a_dying_agent_resolves_the_wait_as_gone(self) -> None:
        # Crash authority on the call the bridge is already making, rather than
        # a hang until the timeout.
        client = FakeClient(
            errors={"agent.wait": herdr.HerdrAPIError("agent_not_found", "nope")}
        )
        status = manager(client).wait(AgentRef("fg-jim-85"), {AgentStatus.IDLE}, 30)
        self.assertEqual(status, AgentStatus.GONE)

    def test_a_timeout_is_an_error(self) -> None:
        client = FakeClient(
            errors={"agent.wait": herdr.HerdrAPIError("timeout", "timed out")}
        )
        with self.assertRaises(AgentError):
            manager(client).wait(AgentRef("fg-jim-85"), {AgentStatus.IDLE}, 1)

    def test_waiting_only_for_gone_is_rejected(self) -> None:
        # GONE has no herdr spelling, so such a wait would send an empty
        # `until` and block forever.
        client = FakeClient()
        with self.assertRaises(AgentError):
            manager(client).wait(AgentRef("fg-jim-85"), {AgentStatus.GONE}, 1)


class StopTests(unittest.TestCase):
    def test_stop_closes_the_whole_workspace(self) -> None:
        client = FakeClient(
            {"agent.get": {"agent": {"workspace_id": "w1", "pane_id": "w1:p1"}}}
        )
        manager(client).stop(AgentRef("fg-jim-85"))
        self.assertEqual(client.params_for("workspace.close")["workspace_id"], "w1")

    def test_stopping_an_absent_agent_is_not_an_error(self) -> None:
        client = FakeClient(
            errors={"agent.get": herdr.HerdrAPIError("agent_not_found", "nope")}
        )
        manager(client).stop(AgentRef("fg-jim-85"))
        self.assertNotIn("workspace.close", client.methods())


class ListAgentsTests(unittest.TestCase):
    def test_only_foregent_agents_are_reported(self) -> None:
        # An operator's own pane in the same session is not ours to reconcile.
        client = FakeClient(
            {
                "agent.list": {
                    "agents": [
                        {"name": "fg-jim-85", "agent_status": "idle", "cwd": "/ws"},
                        {"name": "scratch", "agent_status": "idle"},
                        {"agent_status": "working"},
                    ]
                }
            }
        )
        records = manager(client).list_agents()
        self.assertEqual([r.ref.label for r in records], ["fg-jim-85"])
        self.assertEqual(records[0].status, AgentStatus.IDLE)
        self.assertEqual(records[0].cwd, "/ws")

    def test_a_reported_session_id_is_carried_through(self) -> None:
        # herdr learns the id from Claude Code's SessionStart hook; it is a
        # cross-check on the id foregent assigned at launch.
        client = FakeClient(
            {
                "agent.list": {
                    "agents": [
                        {
                            "name": "fg-jim-85",
                            "agent_status": "idle",
                            "agent_session": {"kind": "id", "value": "abc-123"},
                        }
                    ]
                }
            }
        )
        self.assertEqual(
            manager(client).list_agents()[0].ref.conversation_id, "abc-123"
        )

    def test_a_path_style_session_ref_is_not_a_conversation_id(self) -> None:
        client = FakeClient(
            {
                "agent.list": {
                    "agents": [
                        {
                            "name": "fg-jim-85",
                            "agent_status": "idle",
                            "agent_session": {"kind": "path", "value": "/t.jsonl"},
                        }
                    ]
                }
            }
        )
        self.assertIsNone(manager(client).list_agents()[0].ref.conversation_id)


class EventTests(unittest.TestCase):
    """Translating herdr's pane events into agent events."""

    def setUp(self) -> None:
        patcher = mock.patch("foregent.agents.herdr_claude.RECONNECT_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_with(
        self, stream: list[dict | None], agents: list[dict] | None = None
    ):
        listed = (
            agents
            if agents is not None
            else [{"name": "fg-jim-87", "pane_id": "w1:p1", "workspace_id": "w1"}]
        )
        client = FakeClient({"agent.list": {"agents": listed}})
        client.stream = stream
        return client

    def events(self, client, count: int) -> list:
        """The first ``count`` events, without waiting on the reconnect loop."""
        return list(islice(manager(client).events(), count))

    def test_status_changes_are_subscribed_per_pane(self) -> None:
        # The global pane.updated event carries a PaneInfo whose status lags
        # the agent, so it cannot be used for this.
        client = self.client_with([status_changed("w1:p1", "working")])
        self.events(client, 1)
        subscriptions = client.params_for("events.subscribe")["subscriptions"]
        self.assertIn(
            {"type": "pane.agent_status_changed", "pane_id": "w1:p1"}, subscriptions
        )

    def test_a_status_change_names_the_agent_not_the_pane(self) -> None:
        client = self.client_with([status_changed("w1:p1", "working")])
        [event] = self.events(client, 1)
        self.assertEqual(event.kind, AgentEventKind.STATUS_CHANGED)
        self.assertEqual(event.ref.label, "fg-jim-87")
        self.assertEqual(event.status, AgentStatus.WORKING)

    def test_both_spellings_of_an_event_name_are_understood(self) -> None:
        # herdr sends status changes dotted and everything else underscored;
        # matching one spelling only is how every status update went missing.
        for name in ("pane.agent_status_changed", "pane_agent_status_changed"):
            client = self.client_with([status_changed("w1:p1", "working", name)])
            [event] = self.events(client, 1)
            self.assertEqual(event.status, AgentStatus.WORKING)

    def test_a_repeated_status_is_dropped(self) -> None:
        client = self.client_with(
            [
                status_changed("w1:p1", "working"),
                status_changed("w1:p1", "working"),
                status_changed("w1:p1", "idle"),
            ]
        )
        statuses = [event.status for event in self.events(client, 2)]
        self.assertEqual(statuses, [AgentStatus.WORKING, AgentStatus.IDLE])

    def test_a_closed_pane_is_an_exit(self) -> None:
        client = self.client_with(
            [{"event": "pane_closed", "data": {"pane_id": "w1:p1"}}]
        )
        [event] = self.events(client, 1)
        self.assertEqual(event.kind, AgentEventKind.EXITED)
        self.assertEqual(event.ref.label, "fg-jim-87")
        self.assertEqual(event.status, AgentStatus.GONE)

    def test_an_exited_pane_is_an_exit(self) -> None:
        client = self.client_with(
            [{"event": "pane_exited", "data": {"pane_id": "w1:p1"}}]
        )
        self.assertEqual(self.events(client, 1)[0].kind, AgentEventKind.EXITED)

    def test_a_closed_workspace_is_an_exit(self) -> None:
        # Stopping an agent closes its workspace, and that emits no pane
        # event at all — without this the bridge would never see its own
        # teardowns, or an operator closing a workspace by hand.
        client = self.client_with(
            [{"event": "workspace_closed", "data": {"workspace_id": "w1"}}]
        )
        [event] = self.events(client, 1)
        self.assertEqual(event.kind, AgentEventKind.EXITED)
        self.assertEqual(event.ref.label, "fg-jim-87")

    def test_panes_that_are_not_ours_are_ignored(self) -> None:
        # An operator's own pane in the same session must not look like a
        # foregent agent dying.
        client = self.client_with(
            [
                status_changed("w9:p9", "working"),
                {"event": "pane_closed", "data": {"pane_id": "w9:p9"}},
                status_changed("w1:p1", "idle"),
            ]
        )
        [event] = self.events(client, 1)
        self.assertEqual(event.ref.label, "fg-jim-87")

    def test_a_new_agent_triggers_a_fresh_subscription(self) -> None:
        # Per-pane subscriptions are fixed when the subscription opens, so an
        # agent that starts later can only be watched by re-subscribing.
        client = self.client_with(
            [
                {"event": "pane_agent_detected", "data": {"pane_id": "w2:p1"}},
                status_changed("w2:p1", "working"),
            ]
        )
        # The agent does not exist when the subscription opens; it is there by
        # the time herdr reports having detected it.
        listings = [{"agents": []}]
        later = {"agents": [{"name": "fg-jim-87", "pane_id": "w2:p1", "workspace_id": "w2"}]}
        client.responses["agent.list"] = lambda params: (
            listings.pop(0) if listings else later
        )
        [event] = self.events(client, 1)
        self.assertEqual(event.ref.label, "fg-jim-87")
        self.assertGreaterEqual(client.methods().count("events.subscribe"), 2)

    def test_an_agent_detected_before_it_is_named_is_still_picked_up(self) -> None:
        # herdr announces a detected agent before `agent.start` has finished
        # registering its name, so the refresh that announcement triggers can
        # come back empty. Recording the fleet as up to date at that point
        # would leave the agent's status unwatched for its entire life, with
        # nothing left to notice: the periodic check would see no difference.
        client = FakeClient()
        calls = {"count": 0}
        named = {"agents": [{"name": "fg-jim-87", "pane_id": "w2:p1", "workspace_id": "w2"}]}

        def listing(params: dict) -> dict:
            calls["count"] += 1
            # Nameless while the agent is starting, named shortly after.
            return {"agents": []} if calls["count"] <= 2 else named

        client.responses["agent.list"] = listing
        client.stream = [
            {"event": "pane_agent_detected", "data": {"pane_id": "w2:p1"}},
            None,  # a quiet tick
            status_changed("w2:p1", "working"),
        ]
        [event] = self.events(client, 1)
        self.assertEqual(event.ref.label, "fg-jim-87")
        self.assertEqual(event.status, AgentStatus.WORKING)

    def test_someone_elses_agent_does_not_force_a_re_subscription(self) -> None:
        # Resubscribing on every detected agent would churn the connection
        # for panes the bridge will never watch.
        client = self.client_with(
            [
                {"event": "pane_agent_detected", "data": {"pane_id": "w9:p9"}},
                status_changed("w1:p1", "idle"),
            ]
        )
        [event] = self.events(client, 1)
        self.assertEqual(event.ref.label, "fg-jim-87")
        self.assertEqual(client.methods().count("events.subscribe"), 1)

    def test_the_stream_is_re_established_after_it_drops(self) -> None:
        # A bridge that stopped listening would stop noticing agents dying.
        client = self.client_with([status_changed("w1:p1", "working")])
        self.assertEqual(len(self.events(client, 2)), 2)
        self.assertEqual(client.methods().count("events.subscribe"), 2)

    def test_a_broken_stream_does_not_end_the_iterator(self) -> None:
        client = self.client_with([status_changed("w1:p1", "working")])
        calls = {"count": 0}
        original = client.subscribe

        def subscribe(subscriptions, tick=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise herdr.HerdrTransportError("socket died")
            yield from original(subscriptions, tick)

        client.subscribe = subscribe
        [event] = self.events(client, 1)
        self.assertEqual(event.status, AgentStatus.WORKING)


if __name__ == "__main__":
    unittest.main()
