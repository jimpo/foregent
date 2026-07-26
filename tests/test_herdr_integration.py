"""Integration tests against a real herdr server (JIM-83, JIM-85).

Starts an actual headless ``herdr --session <scratch> server``, drives
:mod:`foregent.herdr` and the manager built on it against that server, and
tears the session down afterward. Skips (does not fail) when the ``herdr``
binary is not on PATH. To run:

    .venv/bin/python -m unittest tests.test_herdr_integration -v

The classes that launch a real Claude Code process need the ``claude`` binary
and working auth on the box. They only start agents and read their state —
they never prompt one — so they cost no API tokens. They are gated separately
behind ``FOREGENT_HERDR_AGENT_TESTS=1``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from foregent import herdr
from foregent.agents import AgentError, AgentStatus, LaunchSpec
from foregent.agents.herdr_claude import HerdrClaudeManager

_HERDR = shutil.which("herdr")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_TESTS = os.environ.get("FOREGENT_HERDR_AGENT_TESTS") == "1"

# Scratch session per test process, so a run never touches the operator's
# own sessions (the real deployment uses the session named "foregent").
_SESSION = f"foregent-inttest-{os.getpid()}"

_READY_TIMEOUT = 20  # seconds to wait for the server to accept connections
_AGENT_START_MS = 90_000  # herdr's own budget for bringing an agent up


def _clean_env() -> dict[str, str]:
    """Environment for the herdr server: no inherited agent state.

    Every pane herdr opens inherits the server's environment, and a leaked
    ``CLAUDECODE`` / ``CLAUDE_CODE_*`` var disables Claude Code's transcript
    saving in the child — which silently breaks resume (docs/PLAN.md §5.8).
    The systemd unit has the same job in production; here it also keeps a
    test run started from inside an agent session honest.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLAUDE", "HERDR", "AI_AGENT"))
    }


def _start_server() -> subprocess.Popen:
    """Start the scratch herdr server and wait for its socket to answer."""
    process = subprocess.Popen(
        [str(_HERDR), "--session", _SESSION, "server"],
        env=_clean_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client = herdr.HerdrClient(session=_SESSION)
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            client.ping()
            return process
        except herdr.HerdrError:
            time.sleep(0.2)
    process.kill()
    raise AssertionError(f"herdr session {_SESSION} did not come up")


def _stop_server(process: subprocess.Popen) -> None:
    """Stop the scratch server and delete its session directory."""
    try:
        herdr.HerdrClient(session=_SESSION).call("server.stop", timeout=10)
    except herdr.HerdrError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    subprocess.run(
        [str(_HERDR), "session", "delete", _SESSION],
        env=_clean_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


@unittest.skipUnless(_HERDR, "herdr is not installed")
class HerdrIntegrationTests(unittest.TestCase):
    """Socket-level behavior, no agents launched."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.process = _start_server()
        cls.client = herdr.HerdrClient(session=_SESSION)

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_server(cls.process)

    def workspace(self, cwd: str, label: str) -> dict:
        """Create a workspace that is closed again when the test ends."""
        created = self.client.call(
            "workspace.create", {"cwd": cwd, "label": label}
        )
        workspace_id = created["workspace"]["workspace_id"]
        self.addCleanup(
            lambda: self.client.call(
                "workspace.close", {"workspace_id": workspace_id}
            )
        )
        return created

    def test_server_speaks_the_expected_protocol(self) -> None:
        # The canary for a herdr upgrade: if this fails, the bridge's own
        # startup check (docs/PLAN.md §5.8) would refuse to start too.
        self.client.check_protocol()

    def test_ping_reports_a_version_and_capabilities(self) -> None:
        banner = self.client.ping()
        self.assertIn("version", banner)
        self.assertIn("capabilities", banner)

    def test_workspace_create_returns_a_root_pane_at_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Resolve symlinks: herdr reports the pane's real cwd, and on
            # macOS /tmp is itself a symlink.
            cwd = str(Path(directory).resolve())
            created = self.workspace(cwd, "JIM-83")
            self.assertEqual(created["workspace"]["label"], "JIM-83")
            pane = created["root_pane"]
            self.assertEqual(pane["cwd"], cwd)

            listed = self.client.call("pane.list")["panes"]
            self.assertIn(pane["pane_id"], [p["pane_id"] for p in listed])

    def test_unknown_method_is_an_api_error(self) -> None:
        with self.assertRaises(herdr.HerdrAPIError) as caught:
            self.client.call("no.such.method")
        self.assertTrue(caught.exception.code)

    def test_server_rejects_a_request_without_params(self) -> None:
        # This is why `call` always sends `params`, even empty: herdr fails
        # the request outright rather than defaulting it.
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        connection.connect(self.client.path)
        with connection:
            connection.sendall(b'{"id":"1","method":"ping"}\n')
            reply = connection.recv(65536)
        self.assertIn(b"invalid_request", reply)


@unittest.skipUnless(_HERDR, "herdr is not installed")
@unittest.skipUnless(_AGENT_TESTS, "FOREGENT_HERDR_AGENT_TESTS is not set")
class AgentIntegrationTests(unittest.TestCase):
    """Launching a real Claude Code agent in a herdr pane."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.process = _start_server()
        cls.client = herdr.HerdrClient(session=_SESSION)

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_server(cls.process)

    def test_agent_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = self.client.call(
                "workspace.create",
                {"cwd": str(Path(directory).resolve()), "label": "JIM-83"},
            )
            pane_id = created["root_pane"]["pane_id"]
            name = f"fg-inttest-{os.getpid()}"

            started = self.client.call(
                "agent.start",
                {
                    "name": name,
                    "kind": "claude",
                    "pane_id": pane_id,
                    # A foregent-assigned conversation id is what makes an
                    # agent resumable (docs/PLAN.md §5.11); assert it reaches
                    # the process rather than trusting the flag list.
                    "args": [
                        "--session-id",
                        str(uuid.uuid4()),
                        "--model",
                        "haiku",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                    "timeout_ms": _AGENT_START_MS,
                },
                timeout=herdr.timeout_for_wait(_AGENT_START_MS),
            )
            self.assertIn("--session-id", started["argv"])

            self.client.call(
                "agent.wait",
                {"target": name, "until": ["idle"], "timeout_ms": _AGENT_START_MS},
                timeout=herdr.timeout_for_wait(_AGENT_START_MS),
            )
            agent = self.client.call("agent.get", {"target": name})["agent"]
            self.assertEqual(agent["agent"], "claude")
            self.assertEqual(agent["agent_status"], "idle")

            read = self.client.call(
                "agent.read", {"target": name, "source": "recent", "lines": 5}
            )
            self.assertIn("text", read["read"])

            # Closing the pane kills the agent, and herdr drops it from the
            # roster at once — the crash authority the bridge relies on
            # (docs/PLAN.md §5.6).
            self.client.call("pane.close", {"pane_id": pane_id})
            names = [
                a.get("name")
                for a in self.client.call("agent.list")["agents"]
            ]
            self.assertNotIn(name, names)


@unittest.skipUnless(_HERDR, "herdr is not installed")
@unittest.skipUnless(_AGENT_TESTS, "FOREGENT_HERDR_AGENT_TESTS is not set")
class ManagerIntegrationTests(unittest.TestCase):
    """HerdrClaudeManager against a real herdr and a real Claude Code."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.process = _start_server()
        cls.manager = HerdrClaudeManager(herdr.HerdrClient(session=_SESSION))

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_server(cls.process)

    def test_launch_list_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = str(Path(directory).resolve())
            label = f"fg-inttest-{os.getpid()}"
            ref = self.manager.launch(
                LaunchSpec(label=label, cwd=cwd, model="haiku")
            )
            self.addCleanup(self.manager.stop, ref)

            # A conversation id exists from the moment the agent does, which
            # is what makes it resumable (docs/PLAN.md §5.11).
            self.assertTrue(ref.conversation_id)
            self.assertEqual(self.manager.status(ref), AgentStatus.IDLE)

            records = self.manager.list_agents()
            self.assertIn(label, [record.ref.label for record in records])
            record = next(r for r in records if r.ref.label == label)
            self.assertEqual(record.cwd, cwd)

            self.assertIn("claude", self.manager.read(ref, lines=40).lower())

            self.manager.stop(ref)
            self.assertEqual(self.manager.status(ref), AgentStatus.GONE)
            self.assertNotIn(
                label, [r.ref.label for r in self.manager.list_agents()]
            )

    def test_a_prompt_sent_right_after_launch_is_answered(self) -> None:
        # The acceptance criterion for launch + send: no settle guesswork, no
        # `agent_prompt_stalled`, no lost first message. Runs in the repo
        # checkout because it is a directory Claude Code already trusts —
        # see the next test for what a fresh one costs.
        label = f"fg-prompt-{os.getpid()}"
        ref = self.manager.launch(
            LaunchSpec(label=label, cwd=str(_REPO_ROOT), model="haiku")
        )
        self.addCleanup(self.manager.stop, ref)
        self.manager.send(ref, "Reply with just the word PONG.")
        self.manager.wait(ref, {AgentStatus.IDLE, AgentStatus.DONE}, 120)
        self.assertIn("PONG", self.manager.read(ref, lines=40))

    def test_an_untrusted_workspace_costs_a_retry_not_the_message(self) -> None:
        # A directory Claude Code has not been told to trust opens a modal
        # that swallows the first prompt while herdr still reports the agent
        # idle and interactive. The delivery check catches that (herdr
        # answers `agent_prompt_stalled`) and the resend gets through, since
        # the first attempt's Enter dismissed the dialog.
        #
        # Recovering is not a reason to leave it: relying on a modal being
        # dismissed by a keystroke meant for something else is fragile, and a
        # dialog whose default is "No, exit" would kill the agent instead.
        # Pre-accepting trust for the workspace pool root is the real fix
        # (docs/PLAN.md §5.8, JIM-96).
        with tempfile.TemporaryDirectory() as directory:
            label = f"fg-untrusted-{os.getpid()}"
            ref = self.manager.launch(
                LaunchSpec(
                    label=label,
                    cwd=str(Path(directory).resolve()),
                    model="haiku",
                )
            )
            self.addCleanup(self.manager.stop, ref)
            self.manager.send(ref, "Reply with just the word PONG.")
            self.manager.wait(ref, {AgentStatus.IDLE, AgentStatus.DONE}, 120)
            self.assertIn("PONG", self.manager.read(ref, lines=40))

    def test_a_second_launch_for_one_issue_is_refused(self) -> None:
        # The deterministic label is what makes a double dispatch impossible
        # rather than merely unlikely (docs/PLAN.md §5.11).
        with tempfile.TemporaryDirectory() as directory:
            cwd = str(Path(directory).resolve())
            label = f"fg-dup-{os.getpid()}"
            spec = LaunchSpec(label=label, cwd=cwd, model="haiku")
            ref = self.manager.launch(spec)
            self.addCleanup(self.manager.stop, ref)
            with self.assertRaises(AgentError):
                self.manager.launch(spec)


if __name__ == "__main__":
    unittest.main()
