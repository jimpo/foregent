"""Integration tests for foregent.cao against a real cao-server instance.

Launches an actual ``cao-server`` subprocess on a free localhost port and
drives ``foregent.cao``'s ``create_session``/``send_message``/
``delete_session`` against it, plus the connection-refused/timeout error
path. Skips (does not fail) when the ``cao-server`` binary is not on PATH.
To run:

    .venv/bin/python -m unittest tests.test_cao_integration -v

``test_session_lifecycle`` launches a real Claude Code CLI session (provider
``claude_code``, model ``haiku``) under a throwaway agent profile, so it
needs the `claude` binary on PATH and takes a few seconds.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from foregent import cao

_CAO_SERVER = shutil.which("cao-server")
_REPO_ROOT = Path(__file__).resolve().parent.parent

# claude_code is the only provider with a working CLI in this environment
# (kiro_cli, cao-server's own default, has no CLI installed here). CAO
# resolves an agent profile's provider by reading a Markdown file out of its
# *local agent store*, a fixed, shared directory
# (~/.aws/cli-agent-orchestrator/agent-store) that is always searched first,
# regardless of --agents-dir. A throwaway profile is written there for the
# duration of this run and removed afterward.
_AGENT_STORE_DIR = Path.home() / ".aws" / "cli-agent-orchestrator" / "agent-store"
_AGENT_PROFILE_NAME = f"foregent-cao-inttest-{os.getpid()}"
_AGENT_PROFILE = f"""---
name: {_AGENT_PROFILE_NAME}
description: Throwaway profile for foregent's cao integration tests
provider: claude_code
model: haiku
allowedTools: []
---

# Test agent

You are a no-op test agent used only by foregent's CAO integration tests.
"""

_READY_TIMEOUT = 15  # seconds to wait for cao-server to accept connections


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_until_ready(deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{cao.api_url()}/sessions", timeout=1)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise TimeoutError("cao-server did not become ready in time")


@unittest.skipUnless(_CAO_SERVER, "cao-server binary not found on PATH")
class CaoIntegrationTests(unittest.TestCase):
    """Round-trips foregent.cao against a real cao-server + Claude Code session."""

    @classmethod
    def setUpClass(cls) -> None:
        assert _CAO_SERVER is not None  # guaranteed by the skipUnless above
        port = _free_port()

        _AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)
        profile_path = _AGENT_STORE_DIR / f"{_AGENT_PROFILE_NAME}.md"
        profile_path.write_text(_AGENT_PROFILE)
        cls.addClassCleanup(profile_path.unlink, missing_ok=True)

        env_patcher = mock.patch.dict(
            os.environ, {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": str(port)}
        )
        env_patcher.start()
        cls.addClassCleanup(env_patcher.stop)

        proc = subprocess.Popen(
            [_CAO_SERVER, "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop_server, proc)

        _wait_until_ready(time.monotonic() + _READY_TIMEOUT)

    @staticmethod
    def _stop_server(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def _delete_session_quietly(self, session_name: str) -> None:
        try:
            cao.delete_session(session_name)
        except (urllib.error.URLError, OSError):
            pass

    def test_session_lifecycle(self) -> None:
        terminal = cao.create_session(_AGENT_PROFILE_NAME, str(_REPO_ROOT))
        self.assertIn("id", terminal)
        self.assertIn("session_name", terminal)
        self.addCleanup(self._delete_session_quietly, terminal["session_name"])

        cao.send_message(terminal["id"], "hello from the integration test")

        cao.delete_session(terminal["session_name"])

        with self.assertRaises((urllib.error.URLError, OSError)) as cm:
            cao.send_message(terminal["id"], "should fail: session is gone")
        if isinstance(cm.exception, urllib.error.HTTPError):
            cm.exception.close()


class CaoUnreachableServerTests(unittest.TestCase):
    """Exercises the error path without needing a real cao-server, so this
    class always runs (unlike CaoIntegrationTests, it needs no binary)."""

    def setUp(self) -> None:
        closed_port = _free_port()  # nothing listens here once the socket closes
        patcher = mock.patch.dict(
            os.environ, {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": str(closed_port)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unreachable_server_fails_fast(self) -> None:
        start = time.monotonic()
        with self.assertRaises((urllib.error.URLError, OSError)):
            cao.create_session("whatever", "/tmp")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, cao.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
