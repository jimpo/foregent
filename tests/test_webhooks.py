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

The GitHub half (JIM-141) is the same subjects over a payload of its own:
which deliveries the endpoint accepts, what one maps to, and who it reaches.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import queue
import threading
import time
import unittest
from collections import deque
from datetime import datetime
from unittest import mock

from fastapi.testclient import TestClient

from foregent import github, linear, server
from foregent.agents import AgentRef
from foregent.events import EventKind
from foregent.models import Issue, IssueStatus, Mode
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


class WebhookFreshTests(unittest.TestCase):
    """The replay window, which is what makes a signature mean *now* (JIM-135)."""

    def stamped(self, ago: float) -> dict:
        """A payload saying Linear sent it ``ago`` seconds before now."""
        return {"webhookTimestamp": (time.time() - ago) * 1000}

    def test_a_delivery_sent_just_now_is_fresh(self) -> None:
        self.assertTrue(linear.webhook_fresh(self.stamped(0)))

    def test_one_inside_the_window_is_fresh(self) -> None:
        self.assertTrue(linear.webhook_fresh(self.stamped(linear.WINDOW - 1)))

    def test_one_older_than_the_window_is_not(self) -> None:
        self.assertFalse(linear.webhook_fresh(self.stamped(linear.WINDOW + 1)))

    def test_one_dated_ahead_of_the_window_is_not(self) -> None:
        # A clock the wrong way out is as much a reason to refuse.
        self.assertFalse(linear.webhook_fresh(self.stamped(-linear.WINDOW - 1)))

    def test_a_delivery_that_names_no_time_is_fresh(self) -> None:
        # The signature covers the exact bytes, so a body without the field is
        # one Linear sent that way, not one stripped of it on the way here.
        self.assertTrue(linear.webhook_fresh({"type": "Comment"}))


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
        # The recent-delivery memory is process-wide, and these tests replay
        # one body across several of them; each starts with an empty one.
        self.enterContext(
            mock.patch.object(server, "_recent", deque(maxlen=server.RECENT_DELIVERIES))
        )
        # As is the last-delivery mark, which starts each test unset.
        self.enterContext(mock.patch.object(server, "_last_delivery", ""))

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

    def test_a_replayed_delivery_is_refused_though_it_is_signed(self) -> None:
        # The signature still holds: Linear did send these bytes, an hour ago.
        stale = dict(PAYLOAD, webhookTimestamp=(time.time() - 3600) * 1000)
        body = json.dumps(stale).encode()
        self.assertEqual(self.post(body, sign(body)).status_code, 400)

    def test_the_same_delivery_sent_now_is_accepted(self) -> None:
        fresh = dict(PAYLOAD, webhookTimestamp=time.time() * 1000)
        body = json.dumps(fresh).encode()
        self.assertEqual(self.post(body, sign(body)).status_code, 200)


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
        # The recent-delivery memory is process-wide, and these tests replay
        # one body across several of them; each starts with an empty one.
        self.enterContext(
            mock.patch.object(server, "_recent", deque(maxlen=server.RECENT_DELIVERIES))
        )
        # As is the last-delivery mark, which starts each test unset.
        self.enterContext(mock.patch.object(server, "_last_delivery", ""))

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

    def last_delivery(self) -> str:
        """When the server says Linear last delivered to it."""
        return self.client.get("/health").json()["last_linear_delivery"]

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

    def logs_from(self, payload: dict) -> str:
        """Everything the server logged at debug while delivering ``payload``."""
        with self.assertLogs("foregent.server", "DEBUG") as logs:
            self.deliver(payload)
        return "\n".join(logs.output)

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

    def test_a_delivered_event_logs_what_reached_which_agent(self) -> None:
        # An operator running at debug can account for any delivery: this one
        # arrived, and this is the prompt the agent was handed for it.
        self.track()
        logs = self.logs_from(self.comment())
        self.assertIn("Linear delivered a create Comment", logs)
        self.assertIn("delivered to JIM-133: AJ commented on JIM-133.", logs)

    def test_a_filtered_delivery_says_it_was_filtered(self) -> None:
        self.track()
        self.assertIn(
            "foregent's own write", self.logs_from(self.comment(actor=self.VIEWER))
        )

    def test_a_delivery_that_is_not_understood_is_summarized(self) -> None:
        logs = self.logs_from({"action": "create", "type": "Comment", "data": {}})
        self.assertIn("Linear delivered a create Comment", logs)
        self.assertIn("about no issue foregent knows", logs)

    def test_a_retried_delivery_prompts_the_agent_once(self) -> None:
        # Linear retries a delivery it believes failed; the worker must not
        # read the same comment twice.
        self.track()
        payload = self.comment()
        first = self.deliver(payload)
        second = self.deliver(payload)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(len(self.manager.sent), 1)

    def test_a_second_comment_is_not_mistaken_for_a_retry(self) -> None:
        # Two deliveries differ in their bodies, so neither hides the other.
        self.track()
        self.deliver(self.comment())
        self.deliver(dict(self.comment(), webhookTimestamp=time.time() * 1000))
        self.assertEqual(len(self.manager.sent), 2)

    def test_only_the_most_recent_deliveries_are_remembered(self) -> None:
        # The memory is bounded, so a repeat is only dropped while it is
        # recent. Nothing else keeps this process from growing on traffic.
        self.enterContext(mock.patch.object(server, "_recent", deque(maxlen=1)))
        self.track()
        payload = self.comment()
        self.deliver(payload)
        self.deliver(dict(payload, webhookTimestamp=time.time() * 1000))
        self.deliver(payload)
        self.assertEqual(len(self.manager.sent), 3)

    def test_when_linear_last_delivered_is_readable(self) -> None:
        # Nothing else on the surface separates a hook that has stopped from a
        # quiet morning, so an authentic delivery has to leave a mark.
        self.assertEqual(self.last_delivery(), "")
        self.deliver(self.comment())
        self.assertAlmostEqual(
            datetime.fromisoformat(self.last_delivery()).timestamp(),
            time.time(),
            delta=60,
        )

    def test_a_delivery_that_reaches_nobody_still_counts_as_one(self) -> None:
        # Liveness is the webhook's, not any agent's: most of what Linear
        # sends is about issues nobody here is working, and that is a
        # delivering hook all the same.
        self.deliver(self.comment(key="JIM-999"))
        self.assertNotEqual(self.last_delivery(), "")

    def test_a_delivery_that_proves_nothing_does_not_count(self) -> None:
        self.client.post("/webhooks/linear", content=b"{}")
        self.assertEqual(self.last_delivery(), "")

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


# A pull request an agent opened for JIM-141, on the branch Linear named after
# the issue and links the pull request by.
BRANCH = "aj/jim-141-deliver-github-pr-updates-to-workers"
AGENT_LOGIN = "foregent-bot"


def review(
    *,
    branch: str = BRANCH,
    sender: str = "jimpo",
    state: str = "changes_requested",
    body: str = "rename this",
) -> dict:
    return {
        "action": "submitted",
        "review": {"state": state, "body": body},
        "pull_request": {
            "number": 9,
            "head": {"ref": branch},
            "user": {"login": AGENT_LOGIN},
        },
        "repository": {"full_name": "jimpo/foregent"},
        "sender": {"login": sender},
    }


def push(*, ref: str = "refs/heads/main", **overrides) -> dict:
    payload = {
        "ref": ref,
        "commits": [
            {"message": "Rebase onto main before PR (JIM-167) (#12)\n\nBody."},
            {"message": "Add --log-level to foregent serve (JIM-149) (#9)"},
        ],
        "repository": {"full_name": "jimpo/foregent"},
        "sender": {"login": "jimpo"},
    }
    payload.update(overrides)
    return payload


def review_comment(**overrides) -> dict:
    payload = {
        "action": "created",
        "comment": {"path": "src/foregent/github.py", "line": 42, "body": "why?"},
        "pull_request": {
            "number": 9,
            "head": {"ref": BRANCH},
            "user": {"login": AGENT_LOGIN},
        },
        "repository": {"full_name": "jimpo/foregent"},
        "sender": {"login": "jimpo"},
    }
    payload.update(overrides)
    return payload


def conversation_comment(**overrides) -> dict:
    """A comment in the pull request's conversation tab (``issue_comment``).

    GitHub calls the thing commented on an ``issue`` whether it is one or a
    pull request, and names no branch either way.
    """
    payload = {
        "action": "created",
        "comment": {"body": "does this handle a force-push?"},
        "issue": {
            "number": 9,
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/9"},
            "user": {"login": AGENT_LOGIN},
        },
        "repository": {"full_name": "jimpo/foregent"},
        "sender": {"login": "jimpo"},
    }
    payload.update(overrides)
    return payload


class GitHubWebhookEventTests(unittest.TestCase):
    """What a GitHub delivery maps to, before anyone is looked up (JIM-141)."""

    def setUp(self) -> None:
        # A conversation comment names no branch, so mapping one asks GitHub
        # for the pull request. What that call does with a socket is
        # `HeadRefTests`' subject; what the mapping does with its answer is
        # this one's.
        self.head_ref = self.enterContext(
            mock.patch.object(github, "_head_ref", return_value=BRANCH)
        )

    def test_a_submitted_review_carries_its_state_and_what_was_written(self) -> None:
        event = github.webhook_event(review(), "pull_request_review")
        assert event is not None
        self.assertEqual(event.kind, EventKind.PR_REVIEW)
        self.assertEqual(event.issue_key, "JIM-141")
        self.assertEqual(event.repo, "jimpo/foregent")
        self.assertEqual(event.number, 9)
        self.assertEqual(event.author, "jimpo")
        self.assertIn("changes requested", event.body)
        self.assertIn("rename this", event.body)

    def test_an_inline_comment_carries_the_line_it_hangs_off(self) -> None:
        # The agent has to act on the feedback; the file and line are half of
        # knowing what it is about.
        event = github.webhook_event(review_comment(), "pull_request_review_comment")
        assert event is not None
        self.assertEqual(event.issue_key, "JIM-141")
        self.assertIn("src/foregent/github.py:42", event.body)
        self.assertIn("why?", event.body)

    def test_the_issue_comes_from_the_branch_however_it_is_written(self) -> None:
        # Linear lower-cases the key in the branch it names; a key written any
        # other way reads the same.
        self.assertEqual(github.issue_key(BRANCH), "JIM-141")
        self.assertEqual(github.issue_key("JIM-141"), "JIM-141")
        self.assertEqual(github.issue_key("jimpo/fix-the-thing"), "")

    def test_the_pull_request_authors_own_review_comment_maps_to_nothing(self) -> None:
        # The agent opened the pull request, so what it writes there comes
        # back here as an event about its own issue. A wake that causes a
        # write is a loop.
        event = github.webhook_event(
            review_comment(sender={"login": AGENT_LOGIN}), "pull_request_review_comment"
        )
        self.assertIsNone(event)

    def test_a_conversation_comment_is_resolved_by_asking_for_the_branch(
        self,
    ) -> None:
        # The payload names no branch, so the one thing that resolves this
        # delivery to an issue is fetched (JIM-177).
        event = github.webhook_event(conversation_comment(), "issue_comment")
        assert event is not None
        self.assertEqual(event.kind, EventKind.PR_REVIEW)
        self.assertEqual(event.issue_key, "JIM-141")
        self.assertEqual(event.repo, "jimpo/foregent")
        self.assertEqual(event.number, 9)
        self.assertEqual(event.author, "jimpo")
        self.assertIn("force-push", event.body)
        self.head_ref.assert_called_once_with("jimpo/foregent", 9)

    def test_a_conversation_comment_on_an_unresolvable_branch_names_no_issue(
        self,
    ) -> None:
        # A pull request GitHub would not answer for reaches nobody, which is
        # what the delivery did before it was handled at all.
        self.head_ref.return_value = ""
        event = github.webhook_event(conversation_comment(), "issue_comment")
        assert event is not None
        self.assertEqual(event.issue_key, "")

    def test_the_pull_request_authors_own_conversation_comment_maps_to_nothing(
        self,
    ) -> None:
        # Same loop as an agent's own review comment, and dropped before the
        # GitHub call rather than after it.
        event = github.webhook_event(
            conversation_comment(sender={"login": AGENT_LOGIN}), "issue_comment"
        )
        self.assertIsNone(event)
        self.head_ref.assert_not_called()

    def test_a_comment_on_a_plain_issue_maps_to_nothing(self) -> None:
        # GitHub delivers comments on issues and on pull requests under one
        # event; the `pull_request` link is what separates them, and foregent
        # opens no issues.
        plain = conversation_comment()
        del plain["issue"]["pull_request"]
        self.assertIsNone(github.webhook_event(plain, "issue_comment"))
        self.head_ref.assert_not_called()

    def test_a_push_to_main_carries_the_repository_and_what_landed(self) -> None:
        # The base moved under everyone with a pull request open against it,
        # and the subjects are how an agent recognizes its own landing.
        event = github.webhook_event(push(), "push")
        assert event is not None
        self.assertEqual(event.kind, EventKind.MAIN_ADVANCED)
        self.assertEqual(event.repo, "jimpo/foregent")
        self.assertEqual(event.author, "jimpo")
        self.assertIn("(JIM-167) (#12)", event.body)
        self.assertIn("(JIM-149) (#9)", event.body)

    def test_a_push_names_no_issue(self) -> None:
        # It is about a repository. Fanning it out is the bridge's job.
        event = github.webhook_event(push(), "push")
        assert event is not None
        self.assertEqual(event.issue_key, "")

    def test_only_the_subject_of_each_commit_is_carried(self) -> None:
        event = github.webhook_event(push(), "push")
        assert event is not None
        self.assertNotIn("Body.", event.body)

    def test_a_push_is_not_dropped_as_foregents_own(self) -> None:
        # An agent pushes its branch and never main, so a push is not a
        # delivery foregent can cause; there is nothing to compare it to.
        event = github.webhook_event(push(sender={"login": AGENT_LOGIN}), "push")
        self.assertIsNotNone(event)

    def test_a_push_to_another_branch_maps_to_nothing(self) -> None:
        # Agents push their own branches constantly, and none of that moves
        # anybody's base.
        pushed = push(ref=f"refs/heads/{BRANCH}")
        self.assertIsNone(github.webhook_event(pushed, "push"))

    def test_a_tag_being_pushed_maps_to_nothing(self) -> None:
        self.assertIsNone(github.webhook_event(push(ref="refs/tags/v1.0"), "push"))

    def test_main_being_deleted_maps_to_nothing(self) -> None:
        # A deletion names the ref too, and leaves nothing to have advanced.
        self.assertIsNone(github.webhook_event(push(deleted=True), "push"))

    def test_a_push_that_carries_no_commits_still_says_main_moved(self) -> None:
        # The subjects are a convenience; the base having moved is the event.
        event = github.webhook_event(push(commits=[]), "push")
        assert event is not None
        self.assertEqual(event.body, "")

    def test_deliveries_foregent_has_no_use_for_map_to_nothing(self) -> None:
        for kind, payload in (
            # An organization webhook carries every repository's every event.
            ("ping", {"zen": "Design for failure."}),
            # A reviewer amending themselves is not new feedback to act on.
            ("pull_request_review", review() | {"action": "edited"}),
            ("pull_request_review_comment", review_comment(action="deleted")),
            ("issue_comment", conversation_comment(action="edited")),
            # The pull request opening and closing is foregent's own doing.
            ("pull_request", review() | {"action": "opened"}),
        ):
            with self.subTest(kind=kind):
                self.assertIsNone(github.webhook_event(payload, kind))


class HeadRefTests(unittest.TestCase):
    """The bridge's one outbound GitHub call (JIM-177).

    The socket is stubbed: what is under test is that a token is required, the
    answer is read, and no failure escapes — not what GitHub replies.
    """

    def setUp(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0ken"}))
        self.urlopen = self.enterContext(
            mock.patch.object(github.urllib.request, "urlopen")
        )
        self.answer({"head": {"ref": BRANCH}})

    def answer(self, payload: object) -> None:
        self.urlopen.return_value = io.BytesIO(json.dumps(payload).encode())

    def test_the_head_branch_is_read_off_the_pull_request(self) -> None:
        self.assertEqual(github._head_ref("jimpo/foregent", 9), BRANCH)

    def test_the_pull_request_is_asked_for_by_repository_and_number(self) -> None:
        github._head_ref("jimpo/foregent", 9)
        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://api.github.com/repos/jimpo/foregent/pulls/9"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer t0ken")

    def test_without_a_token_nothing_is_asked_and_no_branch_comes_back(self) -> None:
        # An operator's misconfiguration, not a caller's fault, and not worth
        # failing a webhook GitHub would retry into the same wall.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(github._head_ref("jimpo/foregent", 9), "")
        self.urlopen.assert_not_called()

    def test_an_unreachable_api_answers_no_branch(self) -> None:
        self.urlopen.side_effect = OSError("connection refused")
        self.assertEqual(github._head_ref("jimpo/foregent", 9), "")

    def test_an_answer_that_is_not_a_pull_request_answers_no_branch(self) -> None:
        for payload in ({"message": "Not Found"}, [], None):
            with self.subTest(payload=payload):
                self.answer(payload)
                self.assertEqual(github._head_ref("jimpo/foregent", 9), "")


class GitHubDeliveryTest(unittest.TestCase):
    """A bridge with a fake harness, reached over the GitHub webhook route."""

    def setUp(self) -> None:
        self.client = TestClient(server.app)
        self.enterContext(
            mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": SECRET})
        )
        server.store = IssueStore()
        self.manager = FakeManager()
        self.enterContext(mock.patch.object(server, "manager", self.manager))
        self.enterContext(mock.patch.object(server, "deliveries", {}))
        self.viewer = self.enterContext(
            mock.patch.object(server.linear, "viewer_id", return_value="viewer-id")
        )

    def deliver(self, payload: dict, event: str = "pull_request_review"):
        body = json.dumps(payload).encode()
        response = self.client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                github.EVENT_HEADER: event,
                github.SIGNATURE_HEADER: sign_github(body),
            },
        )
        drain_deliveries()
        return response


class GitHubWebhookDeliveryTests(GitHubDeliveryTest):
    """Who a GitHub delivery reaches (JIM-141)."""

    KEY = "JIM-141"
    AGENT = AgentRef("fg-jim-141", "conversation-1")

    def track(self, status: IssueStatus = IssueStatus.IN_PROGRESS) -> None:
        server.store.add(
            Issue(
                key=self.KEY,
                title="",
                status=status,
                blocker="a review" if status is IssueStatus.BLOCKED else "",
                agent=self.AGENT,
            )
        )

    def test_a_review_reaches_the_agent_whose_branch_it_is_on(self) -> None:
        self.track()
        response = self.deliver(review())
        self.assertEqual(response.status_code, 200)
        ref, text = self.manager.sent[0]
        self.assertEqual(ref, self.AGENT)
        self.assertIn("jimpo reviewed jimpo/foregent#9.", text)
        self.assertIn("rename this", text)

    def test_a_review_of_a_parked_agents_pull_request_wakes_and_unblocks_it(
        self,
    ) -> None:
        # The whole point: an agent parked on `a review of the PR` is waiting
        # for exactly this.
        self.track(IssueStatus.BLOCKED)
        self.deliver(review())
        _, text = self.manager.sent[0]
        self.assertIn("Waking", text)
        issue = server.store.get(self.KEY)
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_a_review_on_a_branch_nobody_is_working_is_accepted_and_dropped(
        self,
    ) -> None:
        # An organization webhook carries every pull request in every
        # repository; almost none of them are foregent's business.
        self.track()
        response = self.deliver(review(branch="jimpo/jim-9999-something-else"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_a_conversation_comment_reaches_the_agent_whose_branch_it_is_on(
        self,
    ) -> None:
        # The comment tab is the ordinary way a reviewer says something that
        # hangs off no line, and the bridge used to drop it (JIM-177).
        self.track(IssueStatus.BLOCKED)
        with mock.patch.object(github, "_head_ref", return_value=BRANCH):
            response = self.deliver(conversation_comment(), event="issue_comment")
        self.assertEqual(response.status_code, 200)
        ref, text = self.manager.sent[0]
        self.assertEqual(ref, self.AGENT)
        self.assertIn("Waking", text)
        self.assertIn("does this handle a force-push?", text)

    def test_a_delivery_is_matched_without_asking_linear_anything(self) -> None:
        # The branch is the link, so a GitHub delivery costs no Linear call
        # and is not held up by one that fails.
        self.track()
        self.deliver(review())
        self.assertEqual(self.manager.sent[0][0], self.AGENT)
        self.viewer.assert_not_called()


class PushWakeTests(GitHubDeliveryTest):
    """Who a push to ``main`` wakes (JIM-168).

    The two jj readers are stubbed and everything else is real: what is under
    test is which issues the bridge picks out, not what jj prints for them —
    that is :mod:`tests.test_workspaces`' subject.
    """

    REPO = "/srv/foregent"
    SLUG = "jimpo/foregent"

    def setUp(self) -> None:
        super().setUp()
        self.mode = self.enterContext(
            mock.patch.object(
                server.workspaces, "mode_for", return_value=Mode.PULL_REQUEST
            )
        )
        self.slug = self.enterContext(
            mock.patch.object(server.workspaces, "remote_slug", return_value=self.SLUG)
        )

    def park(
        self,
        key: str,
        *,
        status: IssueStatus = IssueStatus.BLOCKED,
        repo: str | None = None,
    ) -> None:
        server.store.add(
            Issue(
                key=key,
                title="",
                status=status,
                repo=self.REPO if repo is None else repo,
                blocker="a review of the PR" if status is IssueStatus.BLOCKED else "",
                agent=AgentRef(f"fg-{key.lower()}", "conversation-1"),
            )
        )

    def push_to_main(self, **overrides):
        return self.deliver(push(**overrides), event="push")

    def test_a_parked_worker_is_woken_and_told_what_landed(self) -> None:
        self.park("JIM-141")
        response = self.push_to_main()
        self.assertEqual(response.status_code, 200)
        ref, text = self.manager.sent[0]
        self.assertEqual(ref, AgentRef("fg-jim-141", "conversation-1"))
        self.assertIn("Waking", text)
        self.assertIn("main advanced in jimpo/foregent", text)
        self.assertIn("(JIM-167) (#12)", text)

    def test_being_woken_returns_the_worker_to_working(self) -> None:
        # Which is why the skill tells a worker still waiting to report itself
        # blocked again: nothing else would reach it on the next push.
        self.park("JIM-141")
        self.push_to_main()
        issue = server.store.get("JIM-141")
        assert issue is not None
        self.assertEqual(issue.status, IssueStatus.IN_PROGRESS)

    def test_every_parked_worker_on_the_repo_is_woken(self) -> None:
        # The event is about the repository, so it has no one owner.
        self.park("JIM-141")
        self.park("JIM-142")
        self.push_to_main()
        self.assertEqual(
            sorted(ref.label for ref, _ in self.manager.sent),
            ["fg-jim-141", "fg-jim-142"],
        )

    def test_a_push_that_wakes_nobody_says_so(self) -> None:
        # An operator running at debug can tell a push nobody was parked on
        # apart from one the bridge never received.
        with self.assertLogs("foregent.server", "DEBUG") as logs:
            self.push_to_main()
        text = "\n".join(logs.output)
        self.assertIn("GitHub delivered a nameless push", text)
        self.assertIn("no parked agent to wake", text)

    def test_a_working_worker_is_left_alone(self) -> None:
        # It is told to check main before it pushes, so it does not need
        # telling twice (JIM-167).
        self.park("JIM-141", status=IssueStatus.IN_PROGRESS)
        self.push_to_main()
        self.assertEqual(self.manager.sent, [])

    def test_a_bootstrap_worker_is_left_alone(self) -> None:
        # No pull request to go stale, and no remote that could have moved.
        self.mode.return_value = Mode.BOOTSTRAP
        self.park("JIM-141")
        self.push_to_main()
        self.assertEqual(self.manager.sent, [])

    def test_a_worker_on_another_repository_is_left_alone(self) -> None:
        # An organization webhook carries every repository's pushes.
        self.slug.return_value = "jimpo/binius64"
        self.park("JIM-141")
        self.push_to_main()
        self.assertEqual(self.manager.sent, [])

    def test_a_repo_whose_remote_cannot_be_read_is_woken_anyway(self) -> None:
        # Fail open: a spurious wake costs one agent turn, a missed one leaves
        # an agent parked forever on a base that has moved.
        self.slug.return_value = ""
        self.park("JIM-141")
        self.push_to_main()
        self.assertEqual(len(self.manager.sent), 1)

    def test_a_worker_with_no_workspace_is_left_alone(self) -> None:
        # An agent that is not sitting in a workspace names no repo, and
        # bootstrap is the answer foregent gives for it (§6.4).
        self.park("JIM-141", repo="")
        self.push_to_main()
        self.assertEqual(self.manager.sent, [])

    def test_a_push_wakes_nobody_when_nothing_is_parked(self) -> None:
        response = self.push_to_main()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.sent, [])

    def test_a_push_is_matched_without_asking_linear_anything(self) -> None:
        self.park("JIM-141")
        self.push_to_main()
        self.viewer.assert_not_called()
