"""Unit tests for the herdr socket client (JIM-82).

Everything here runs against a fake unix-socket server in-process; the tests
against a real herdr server are gated separately in
``tests.test_herdr_integration``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from foregent import herdr

# A responder answers a decoded request with a JSON envelope, or with raw
# bytes when a test needs to exercise malformed framing.
Responder = Callable[[dict], dict | bytes]


class FakeHerdr:
    """A stand-in herdr server: one canned response per request."""

    def __init__(self, path: str, responder: Responder) -> None:
        self.path = path
        self.responder = responder
        self.requests: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(4)
        # Short accept timeout so the serve loop notices `close()` promptly.
        self._server.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                payload = b""
                while b"\n" not in payload:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    payload += chunk
                if not payload:
                    continue
                request = json.loads(payload.split(b"\n", 1)[0])
                self.requests.append(request)
                reply = self.responder(request)
                # A responder may hand back raw bytes to exercise malformed
                # framing; anything else is a normal JSON envelope.
                if not isinstance(reply, bytes):
                    reply = json.dumps(reply).encode() + b"\n"
                connection.sendall(reply)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()


def ok(result: dict) -> Responder:
    """Responder returning ``result`` for any request."""
    return lambda request: {"id": request["id"], "result": result}


class SocketPathTests(unittest.TestCase):
    """Path resolution, which decides *which* herdr a caller reaches."""

    def test_named_session_wins_over_environment(self) -> None:
        with mock.patch.dict(
            os.environ, {"HERDR_SOCKET_PATH": "/tmp/ignored.sock"}
        ):
            self.assertEqual(
                herdr.socket_path("foregent"),
                str(herdr.CONFIG_DIR / "sessions" / "foregent" / "herdr.sock"),
            )

    def test_socket_path_env_wins_over_session_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HERDR_SOCKET_PATH": "/tmp/explicit.sock", "HERDR_SESSION": "other"},
        ):
            self.assertEqual(herdr.socket_path(), "/tmp/explicit.sock")

    def test_session_env_is_used_when_no_explicit_path(self) -> None:
        environ = {"HERDR_SESSION": "foregent"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(
                herdr.socket_path(),
                str(herdr.CONFIG_DIR / "sessions" / "foregent" / "herdr.sock"),
            )

    def test_default_session_lives_at_the_config_root(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                herdr.socket_path(), str(herdr.CONFIG_DIR / "herdr.sock")
            )
        self.assertEqual(
            herdr.socket_path("default"), str(herdr.CONFIG_DIR / "herdr.sock")
        )

    def test_explicit_path_overrides_everything(self) -> None:
        client = herdr.HerdrClient(session="foregent", path="/tmp/given.sock")
        self.assertEqual(client.path, "/tmp/given.sock")


class WaitTimeoutTests(unittest.TestCase):
    def test_wait_budget_exceeds_the_server_side_wait(self) -> None:
        # The socket must outlast the server's own wait, or the client gives
        # up first and the caller never sees herdr's timeout error.
        self.assertGreater(herdr.timeout_for_wait(120_000), 120)

    def test_absent_wait_falls_back_to_the_default(self) -> None:
        self.assertEqual(herdr.timeout_for_wait(None), herdr.TIMEOUT)


class CallTests(unittest.TestCase):
    """Round trips against the fake server."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / "herdr.sock")

    def serve(self, responder: Responder) -> FakeHerdr:
        server = FakeHerdr(self.path, responder)
        self.addCleanup(server.close)
        return server

    def test_call_returns_the_result_payload(self) -> None:
        self.serve(ok({"type": "pane_list", "panes": []}))
        client = herdr.HerdrClient(path=self.path)
        self.assertEqual(
            client.call("pane.list"), {"type": "pane_list", "panes": []}
        )

    def test_params_are_always_sent(self) -> None:
        # herdr rejects a request with no `params` key outright, so the
        # client must send an empty object rather than omit it.
        server = self.serve(ok({"type": "pong"}))
        herdr.HerdrClient(path=self.path).call("ping")
        self.assertEqual(server.requests[0]["params"], {})

    def test_params_are_forwarded(self) -> None:
        server = self.serve(ok({"type": "ok"}))
        herdr.HerdrClient(path=self.path).call(
            "workspace.create", {"cwd": "/ws/JIM-52", "label": "JIM-52"}
        )
        request = server.requests[0]
        self.assertEqual(request["method"], "workspace.create")
        self.assertEqual(request["params"]["label"], "JIM-52")

    def test_error_response_raises_with_its_code(self) -> None:
        self.serve(
            lambda request: {
                "id": request["id"],
                "error": {
                    "code": "agent_prompt_stalled",
                    "message": "no observed state change",
                },
            }
        )
        client = herdr.HerdrClient(path=self.path)
        with self.assertRaises(herdr.HerdrAPIError) as caught:
            client.call("agent.prompt", {"target": "fg-jim-52", "text": "hi"})
        # The code is what callers switch on: a stalled prompt is recoverable
        # (JIM-86), a transport failure is not.
        self.assertEqual(caught.exception.code, "agent_prompt_stalled")

    def test_response_larger_than_one_recv_is_reassembled(self) -> None:
        scrollback = "x" * 300_000
        self.serve(ok({"type": "pane_read", "read": {"text": scrollback}}))
        client = herdr.HerdrClient(path=self.path)
        result = client.call("agent.read", {"target": "fg-jim-52"})
        self.assertEqual(result["read"]["text"], scrollback)

    def test_non_json_response_is_a_transport_error(self) -> None:
        self.serve(lambda request: b"not json\n")
        client = herdr.HerdrClient(path=self.path)
        with self.assertRaises(herdr.HerdrTransportError):
            client.call("ping")

    def test_response_without_result_is_a_transport_error(self) -> None:
        self.serve(lambda request: {"id": request["id"]})
        client = herdr.HerdrClient(path=self.path)
        with self.assertRaises(herdr.HerdrTransportError):
            client.call("ping")

    def test_unreachable_socket_is_a_transport_error(self) -> None:
        client = herdr.HerdrClient(path=str(Path(self._dir.name) / "absent.sock"))
        with self.assertRaises(herdr.HerdrTransportError):
            client.call("ping")

    def test_check_protocol_accepts_a_matching_server(self) -> None:
        self.serve(ok({"type": "pong", "protocol": herdr.PROTOCOL}))
        herdr.HerdrClient(path=self.path).check_protocol()

    def test_check_protocol_rejects_a_drifted_server(self) -> None:
        self.serve(ok({"type": "pong", "protocol": herdr.PROTOCOL + 1}))
        client = herdr.HerdrClient(path=self.path)
        with self.assertRaises(herdr.HerdrError):
            client.check_protocol()


if __name__ == "__main__":
    unittest.main()
