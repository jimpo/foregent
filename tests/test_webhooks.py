"""Tests for the webhook endpoints (JIM-128, JIM-133, JIM-139).

Driven over HTTP rather than by calling the handler, because the thing under
test is what arrives on the wire: the exact bytes of the body, and a header
the route reads for itself. The client is built without its context manager on
purpose — entering it would run the app's lifespan, which talks to herdr and
starts the delivery threads, neither of which a webhook route has anything to
do with.

The delivery half borrows the fake harness and the drainer from
``tests.test_server``: what a message does once it is queued is that module's
subject, and what is queued for whom is this one's.

The GitHub endpoint is receipt and authentication only, so its tests stop
where it does: who a delivery reaches is JIM-141's subject.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from foregent import github, linear, server
from foregent.agents import AgentRef
from foregent.models import Issue, IssueStatus
from foregent.store import IssueStore
from tests.test_server import FakeManager, drain_deliveries

SECRET = "s3cret"

PAYLOAD = {
    "action": "create",
    "type": "Comment",
    "data": {"body": "ship it", "issue": {"identifier": "JIM-128"}},
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_github(body: bytes, secret: str = SECRET) -> str:
    """The same digest, prefixed the way GitHub names the algorithm it used."""
    return f"sha256={sign(body, secret)}"


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
    """Which deliveries ``POST /webhooks/linear`` accepts at all."""

    def setUp(self) -> None:
        self.client = TestClient(server.app)
        self.enterContext(
            mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": SECRET})
        )
        # An accepted delivery is matched against foregent's own account id,
        # so hand one over rather than have a test of the signature ask
        # Linear for it. The store is empty, so nothing is delivered anyway.
        self.enterContext(mock.patch.object(server, "_viewer", "own-id"))

    def post(self, body: bytes, signature: str | None):
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers[linear.SIGNATURE_HEADER] = signature
        return self.client.post("/webhooks/linear", content=body, headers=headers)

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


class WebhookDeliveryTests(unittest.TestCase):
    """Who a delivery reaches, and what Linear is told about it (JIM-133)."""

    # Foregent's own Linear account, which it writes as on every claim and
    # every comment an agent posts through the Linear MCP.
    VIEWER = "viewer-id"
    KEY = "JIM-133"
    AGENT = AgentRef("fg-jim-133", "conversation-1")

    def setUp(self) -> None:
        self.client = TestClient(server.app)
        self.enterContext(
            mock.patch.dict(os.environ, {"LINEAR_WEBHOOK_SECRET": SECRET})
        )
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        self.enterContext(mock.patch.object(server, "deliveries", {}))
        # The account id is remembered process-wide, so each test starts
        # without one and says for itself whether Linear can be asked.
        self.enterContext(mock.patch.object(server, "_viewer", ""))
        self.viewer = self.enterContext(
            mock.patch.object(server.linear, "viewer_id", return_value=self.VIEWER)
        )

    def track(self, status: IssueStatus = IssueStatus.IN_PROGRESS) -> None:
        """Put ``KEY`` in the store with an agent behind it."""
        server.store.add(
            Issue(
                key=self.KEY,
                title="",
                status=status,
                blocker="a review" if status is IssueStatus.BLOCKED else "",
                agent=self.AGENT,
            )
        )

    def comment(self, key: str = KEY, actor: str = "operator") -> dict:
        return {
            "action": "create",
            "type": "Comment",
            "actor": {"id": actor, "name": "AJ"},
            "data": {"body": "ship it", "issue": {"identifier": key}},
        }

    def deliver(self, payload: dict):
        """Post ``payload`` signed, then hand what it queued to the agents."""
        body = json.dumps(payload).encode()
        response = self.post(body)
        drain_deliveries()
        return response

    def post(self, payload_or_body):
        body = (
            payload_or_body
            if isinstance(payload_or_body, bytes)
            else json.dumps(payload_or_body).encode()
        )
        return self.client.post(
            "/webhooks/linear",
            content=body,
            headers={
                "Content-Type": "application/json",
                linear.SIGNATURE_HEADER: sign(body),
            },
        )

    def test_a_comment_on_a_working_issue_reaches_its_agent(self) -> None:
        self.track()
        response = self.deliver(self.comment())
        self.assertEqual(response.status_code, 200)
        ref, text = self.manager.sent[0]
        self.assertEqual(ref, self.AGENT)
        self.assertIn("AJ commented on JIM-133.", text)
        self.assertIn("ship it", text)

    def test_a_comment_on_a_parked_issue_wakes_and_unblocks_it(self) -> None:
        self.track(IssueStatus.BLOCKED)
        self.deliver(self.comment())
        _, text = self.manager.sent[0]
        self.assertIn("Waking", text)
        issue = server.store.get(self.KEY)
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_a_field_change_reaches_the_agent_as_what_changed(self) -> None:
        # A person answers an agent by moving the issue as often as by writing
        # to it.
        self.track()
        self.deliver(
            {
                "action": "update",
                "type": "Issue",
                "actor": {"id": "operator", "name": "AJ"},
                "data": {"identifier": self.KEY, "state": {"name": "Todo"}},
                "updatedFrom": {"stateId": "state-was"},
            }
        )
        _, text = self.manager.sent[0]
        self.assertIn("AJ updated JIM-133.", text)
        self.assertIn("state: state-was → Todo", text)

    def test_the_route_answers_before_the_agent_is_sent_anything(self) -> None:
        # Linear retries a delivery the bridge is slow to answer, so the route
        # queues and returns; the send waits on the agent elsewhere. Held
        # inside the send, so the route is answering while a delivery it
        # started is provably still outstanding.
        self.track()
        sending = threading.Event()
        holding = threading.Event()
        sent = self.manager.send

        def hold(ref: AgentRef, text: str, *, when_idle: bool = True) -> None:
            sending.set()
            holding.wait(5)
            sent(ref, text, when_idle=when_idle)

        self.enterContext(mock.patch.object(self.manager, "send", hold))
        self.addCleanup(holding.set)

        response = self.post(self.comment())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(sending.wait(5))
        self.assertEqual(self.manager.sent, [])

    def test_a_comment_on_an_untracked_issue_reaches_nobody_and_is_accepted(
        self,
    ) -> None:
        # Most of what Linear sends is about somebody else's issue. Foregent
        # must not report a delivery failed for being none of its business.
        response = self.deliver(self.comment(key="JIM-9999"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_a_comment_on_an_issue_with_no_agent_reaches_nobody(self) -> None:
        server.store.add(
            Issue(key=self.KEY, title="", status=IssueStatus.IN_PROGRESS)
        )
        self.assertEqual(self.deliver(self.comment()).status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_foregents_own_comment_reaches_nobody(self) -> None:
        # Every comment an agent posts through the Linear MCP comes straight
        # back here. A wake that causes a write is a loop.
        self.track()
        response = self.deliver(self.comment(actor=self.VIEWER))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_a_delivery_about_no_issue_is_accepted_and_dropped(self) -> None:
        self.track()
        response = self.deliver({"action": "create", "type": "Cycle", "data": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_a_signed_body_that_is_not_a_delivery_is_rejected(self) -> None:
        # Nothing Linear sends is anything but a JSON object; a 400 says so
        # rather than pretending the delivery was handled.
        self.assertEqual(self.post(b"not json").status_code, 400)
        self.assertEqual(self.post(b"[]").status_code, 400)

    def test_a_delivery_is_refused_while_foregents_own_id_is_unknown(self) -> None:
        # Matching without it would wake an agent with its own comment, so
        # the delivery is not accepted — Linear retries it.
        self.track()
        self.viewer.side_effect = linear.LinearError("no API key")
        with self.assertLogs(server.logger, "ERROR"):
            response = self.post(self.comment())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(server.deliveries, {})

    def test_foregents_own_id_is_asked_for_once(self) -> None:
        self.track()
        self.deliver(self.comment())
        self.deliver(self.comment())
        self.viewer.assert_called_once()


class GitHubWebhookAuthenticTests(unittest.TestCase):
    """The signature check itself (JIM-139)."""

    def setUp(self) -> None:
        self.enterContext(
            mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": SECRET})
        )

    def test_a_signature_over_the_body_is_authentic(self) -> None:
        body = b'{"action":"opened"}'
        self.assertTrue(github.webhook_authentic(body, sign_github(body)))

    def test_a_signature_for_other_bytes_is_not(self) -> None:
        # The delivery GitHub signed is not the one that arrived.
        self.assertFalse(
            github.webhook_authentic(b'{"action":"closed"}', sign_github(b"{}"))
        )

    def test_another_secret_is_not(self) -> None:
        body = b"{}"
        self.assertFalse(github.webhook_authentic(body, sign_github(body, "wrong")))

    def test_the_digest_alone_is_not(self) -> None:
        # GitHub always names the algorithm, so a bare hex digest is not a
        # signature it sent — and comparing without the prefix would accept
        # the same digest under any algorithm a caller cared to claim.
        body = b"{}"
        self.assertFalse(github.webhook_authentic(body, sign(body)))

    def test_an_unset_secret_refuses_rather_than_accepts(self) -> None:
        # A bridge that cannot check a signature must not wave the body
        # through; the caller turns this into a 503.
        with mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": ""}):
            with self.assertRaises(github.GitHubError):
                github.webhook_authentic(b"{}", "")


class GitHubWebhookRouteTests(unittest.TestCase):
    """Which deliveries ``POST /webhooks/github`` accepts at all (JIM-139)."""

    PULL_REQUEST = {
        "action": "submitted",
        "pull_request": {"number": 3},
        "repository": {"full_name": "jimpo/foregent"},
    }

    def setUp(self) -> None:
        self.client = TestClient(server.app)
        self.enterContext(
            mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": SECRET})
        )

    def post(self, body: bytes, signature: str | None, event: str = "pull_request"):
        headers = {"Content-Type": "application/json", github.EVENT_HEADER: event}
        if signature is not None:
            headers[github.SIGNATURE_HEADER] = signature
        return self.client.post("/webhooks/github", content=body, headers=headers)

    def test_the_signature_is_checked_against_the_bytes_that_arrived(self) -> None:
        # Signing a re-serialization of the payload instead of the delivery
        # is the mistake this route must not make: same JSON, different
        # whitespace, different digest.
        body = json.dumps(self.PULL_REQUEST, indent=2).encode()
        compact = sign_github(json.dumps(self.PULL_REQUEST).encode())
        self.assertEqual(self.post(body, compact).status_code, 401)
        self.assertEqual(self.post(body, sign_github(body)).status_code, 200)

    def test_a_forged_delivery_is_rejected(self) -> None:
        forged = self.post(b'{"action":"opened"}', sign_github(b"{}"))
        self.assertEqual(forged.status_code, 401)

    def test_a_delivery_with_no_signature_is_rejected(self) -> None:
        # Absent proves as little as wrong does.
        self.assertEqual(self.post(b"{}", None).status_code, 401)

    def test_an_unconfigured_bridge_says_so_rather_than_accepting(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": ""}):
            with self.assertLogs(server.logger, "ERROR"):
                response = self.post(b"{}", sign_github(b"{}"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("GITHUB_WEBHOOK_SECRET", response.json()["detail"])

    def test_the_ping_that_confirms_the_hook_is_accepted(self) -> None:
        # GitHub sends this when the webhook is created; accepting it is what
        # tells an operator the endpoint is wired up.
        body = json.dumps({"zen": "Design for failure.", "hook_id": 1}).encode()
        response = self.post(body, sign_github(body), event="ping")
        self.assertEqual(response.status_code, 200)

    def test_a_delivery_about_a_pull_request_nobody_is_working_is_accepted(
        self,
    ) -> None:
        # An organization webhook carries every repository; a failure code
        # would buy retries of an event that would be dropped again.
        body = json.dumps(self.PULL_REQUEST).encode()
        self.assertEqual(self.post(body, sign_github(body)).status_code, 200)

    def test_a_signed_body_that_is_not_a_delivery_is_rejected(self) -> None:
        # What a webhook set to form-encoded delivery sends, and what nothing
        # else GitHub sends looks like.
        form = b"payload=%7B%7D"
        self.assertEqual(self.post(form, sign_github(form)).status_code, 400)
        self.assertEqual(self.post(b"[]", sign_github(b"[]")).status_code, 400)


if __name__ == "__main__":
    unittest.main()
