"""Integration tests for foregent.linear against the real Linear API.

Gated on env vars so they never run by default (including in CI). To run:

    LINEAR_API_KEY=... LINEAR_TEST_ISSUE_ID=JIM-XX \\
        .venv/bin/python -m unittest tests.test_linear_integration

The test issue's status will be changed arbitrarily by the round-trip test.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from foregent import linear

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


if __name__ == "__main__":
    unittest.main()
