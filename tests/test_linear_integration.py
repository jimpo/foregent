"""Integration tests for foregent.linear against the real Linear API.

Gated on env vars so they never run by default (including in CI). To run:

    LINEAR_API_KEY=... LINEAR_TEST_ISSUE_ID=JIM-XX \\
        .venv/bin/python -m unittest tests.test_linear_integration

The test issue's status will be changed arbitrarily by the round-trip test.

The classes at the bottom need no API key: response handling is worth pinning
where CI can see it, and only the query shapes need a live workspace.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from foregent import linear
from foregent.events import EventKind

_ISSUE = os.environ.get("LINEAR_TEST_ISSUE_ID", "")
_ENABLED = bool(_ISSUE and os.environ.get("LINEAR_API_KEY"))

_STATE_QUERY = """
query IssueState($key: String!) {
  viewer { id }
  issue(id: $key) {
    id
    state { id name }
    assignee { id }
    team {
      states {
        nodes { id name }
      }
    }
  }
}
"""

_SET_STATE_MUTATION = """
mutation SetState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
  }
}
"""


@unittest.skipUnless(_ENABLED, "set LINEAR_API_KEY and LINEAR_TEST_ISSUE_ID to run")
class LinearIntegrationTests(unittest.TestCase):
    def test_claim_issue_round_trip(self) -> None:
        data = linear._request(_STATE_QUERY, {"key": _ISSUE})
        issue = data["issue"]
        states = issue["team"]["states"]["nodes"]
        other_state = next((s for s in states if s["name"] != "In Progress"), None)
        if other_state is None:
            self.skipTest("team has no non-'In Progress' state to start from")

        set_result = linear._request(
            _SET_STATE_MUTATION, {"id": issue["id"], "stateId": other_state["id"]}
        )
        self.assertTrue(set_result["issueUpdate"]["success"])

        data = linear._request(_STATE_QUERY, {"key": _ISSUE})
        self.assertNotEqual(data["issue"]["state"]["name"], "In Progress")

        linear.claim_issue(_ISSUE)

        data = linear._request(_STATE_QUERY, {"key": _ISSUE})
        issue = data["issue"]
        self.assertEqual(issue["state"]["name"], "In Progress")
        self.assertEqual(issue["assignee"]["id"], data["viewer"]["id"])

    def test_claim_unknown_issue_raises(self) -> None:
        with self.assertRaises(linear.LinearError):
            linear.claim_issue("ZZZ-99999999")

    def test_polling_accepts_issue_identifiers_and_its_own_cursor(self) -> None:
        # The two things only a live workspace can confirm about the poll
        # query (JIM-36): that the comment filter takes identifiers like
        # `JIM-36` as `issue(id:)` does, and that a cursor Linear itself
        # issued round-trips — `gt` against it must exclude the comment it
        # names, or every tick would re-serve one and wake an agent twice.
        events, cursor = linear.poll_comments([_ISSUE], "-P90D", linear.viewer_id())
        self.assertTrue(all(e.issue_key == _ISSUE for e in events))
        if not events:
            self.skipTest(f"{_ISSUE} has no comments from anyone but foregent")
        repeat, repeat_cursor = linear.poll_comments([_ISSUE], cursor)
        self.assertEqual(repeat, [])
        self.assertEqual(repeat_cursor, cursor)


class LinearMissingApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop("LINEAR_API_KEY", None)

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_claim_issue_without_api_key_raises(self) -> None:
        with self.assertRaises(linear.LinearError):
            linear.claim_issue("JIM-1")


def _node(key: str, created: str, body: str = "") -> dict:
    return {
        "id": f"comment-{created}",
        "createdAt": created,
        "body": body,
        "user": {"id": "u1", "name": "Jim Posen"},
        "issue": {"identifier": key},
    }


class PollCommentsTests(unittest.TestCase):
    """Turning a Linear response into events and a cursor (JIM-36)."""

    def poll(self, nodes: list[dict], since: str = "T0", viewer: str = "v"):
        with mock.patch.object(
            linear, "_request", return_value={"comments": {"nodes": nodes}}
        ) as request:
            self.request = request
            return linear.poll_comments(["JIM-36"], since, viewer)

    def test_a_comment_becomes_an_event_about_its_own_issue(self) -> None:
        events, _ = self.poll([_node("JIM-36", "T1", body="the schema is wrong")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.COMMENT)
        self.assertEqual(events[0].issue_key, "JIM-36")
        self.assertEqual(events[0].actor, "u1")
        self.assertEqual(events[0].author, "Jim Posen")
        self.assertEqual(events[0].body, "the schema is wrong")

    def test_the_cursor_becomes_the_last_comment_served(self) -> None:
        # Nodes arrive oldest-first (the query asks for `last`), so the last
        # one is the newest and the next window starts after it.
        _, cursor = self.poll([_node("JIM-36", "T1"), _node("JIM-36", "T2")])
        self.assertEqual(cursor, "T2")

    def test_a_quiet_window_hands_back_the_cursor_it_was_given(self) -> None:
        # Never a clock reading: a comment written while this very query was
        # in flight would fall in the gap.
        events, cursor = self.poll([], since="T0")
        self.assertEqual((events, cursor), ([], "T0"))

    def test_an_automated_comment_has_an_actor_of_nobody(self) -> None:
        # Linear attributes some comments to no user at all. That is nobody's
        # own write, which is exactly what `wakes` reads an empty actor as.
        events, _ = self.poll([_node("JIM-36", "T1") | {"user": None}])
        self.assertEqual((events[0].actor, events[0].author), ("", ""))

    def test_the_viewer_is_sent_so_linear_drops_foregents_writes(self) -> None:
        self.poll([], viewer="viewer-id")
        self.assertEqual(self.request.call_args.args[1]["viewer"], "viewer-id")

    def test_no_tracked_issues_means_no_request_at_all(self) -> None:
        # An empty key list filters nothing in Linear, so asking would return
        # the whole workspace's comments.
        with mock.patch.object(linear, "_request") as request:
            self.assertEqual(linear.poll_comments([], "T0", "v"), ([], "T0"))
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
