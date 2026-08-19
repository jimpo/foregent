"""Tests for the Linear webhook endpoint (JIM-128).

Driven over HTTP rather than by calling the handler, because the thing under
test is what arrives on the wire: the exact bytes of the body, and a header
the route reads for itself. The client is built without its context manager on
purpose — entering it would run the app's lifespan, which talks to herdr and
starts the poll thread, neither of which a webhook has anything to do with.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from foregent import linear, server

SECRET = "s3cret"

PAYLOAD = {
    "action": "create",
    "type": "Comment",
    "data": {"body": "ship it", "issue": {"identifier": "JIM-128"}},
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookAuthenticTests(unittest.TestCase):
    """The signature check itself."""

    def setUp(self) -> None:
        self.enterContext(
            mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": SECRET})
        )

    def test_a_signature_over_the_body_is_authentic(self) -> None:
        body = b'{"action":"create"}'
        self.assertTrue(linear.webhook_authentic(body, sign(body)))

    def test_a_signature_for_other_bytes_is_not(self) -> None:
        # The delivery Linear signed is not the one that arrived.
        self.assertFalse(linear.webhook_authentic(b'{"action":"remove"}', sign(b"{}")))

    def test_another_secret_is_not(self) -> None:
        body = b"{}"
        self.assertFalse(linear.webhook_authentic(body, sign(body, "wrong")))

    def test_an_unset_secret_refuses_rather_than_accepts(self) -> None:
        # A bridge that cannot check a signature must not wave the body
        # through; the caller turns this into a 503.
        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": ""}):
            with self.assertRaises(linear.LinearError):
                linear.webhook_authentic(b"{}", "")


class WebhookRouteTests(unittest.TestCase):
    """What ``POST /webhooks/linear`` does with a delivery."""

    def setUp(self) -> None:
        self.client = TestClient(server.app)
        self.enterContext(
            mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": SECRET})
        )

    def post(self, body: bytes, signature: str | None):
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers[linear.SIGNATURE_HEADER] = signature
        return self.client.post("/webhooks/linear", content=body, headers=headers)

    def test_an_authentic_delivery_is_accepted_and_logged(self) -> None:
        body = json.dumps(PAYLOAD).encode()
        with self.assertLogs(server.logger, "INFO") as logs:
            response = self.post(body, sign(body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        # Whole body, not a summary: nothing maps a Linear payload to an
        # Event yet, so the log is what that mapping gets designed against.
        self.assertIn(body.decode(), "\n".join(logs.output))

    def test_the_signature_is_checked_against_the_bytes_that_arrived(self) -> None:
        # Signing a re-serialization of the payload instead of the delivery
        # is the mistake this route must not make: same JSON, different
        # whitespace, different digest.
        body = json.dumps(PAYLOAD, indent=2).encode()
        compact = sign(json.dumps(PAYLOAD).encode())
        self.assertEqual(self.post(body, compact).status_code, 401)
        self.assertEqual(self.post(body, sign(body)).status_code, 200)

    def test_a_forged_delivery_is_rejected(self) -> None:
        forged = self.post(b'{"action":"create"}', sign(b"{}"))
        self.assertEqual(forged.status_code, 401)

    def test_a_delivery_with_no_signature_is_rejected(self) -> None:
        # Absent proves as little as wrong does.
        self.assertEqual(self.post(b"{}", None).status_code, 401)

    def test_an_unconfigured_bridge_says_so_rather_than_accepting(self) -> None:
        with mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": ""}):
            with self.assertLogs(server.logger, "ERROR"):
                response = self.post(b"{}", sign(b"{}"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("LINEAR_WEBHOOK_SECRET", response.json()["detail"])

    def test_a_delivery_wakes_nobody(self) -> None:
        # The tick is the only delivery path (docs/PLAN.md §5.1); this
        # endpoint is a sink until Q8 says otherwise.
        body = json.dumps(PAYLOAD).encode()
        with mock.patch.object(server, "manager") as manager:
            self.post(body, sign(body))
        manager.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
