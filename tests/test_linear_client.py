"""Tests for the calls the bridge makes to Linear directly (JIM-200).

The transport is mocked out: what is worth guarding here is which mutation
foregent decides to send, not that ``gql`` can send one. The round trip
against the real API is :mod:`tests.test_linear_integration`.
"""

from __future__ import annotations

import unittest
from unittest import mock

from foregent import linear

DONE = {
    "issue": {
        "id": "issue-1",
        "state": {"type": "started"},
        "team": {"states": {"nodes": [{"id": "state-done"}]}},
    }
}


def closing(state: str) -> list[dict]:
    """The two replies a :func:`linear.close_issue` call reads, for ``state``."""
    issue = {**DONE["issue"], "state": {"type": state}}
    return [{"issue": issue}, {"issueUpdate": {"success": True}}]


class CloseIssueTests(unittest.TestCase):
    def test_close_moves_the_issue_to_done(self) -> None:
        with mock.patch.object(
            linear, "_request", side_effect=closing("started")
        ) as request:
            linear.close_issue("JIM-88")

        query, variables = request.call_args.args
        self.assertIn("issueUpdate", query)
        self.assertEqual(variables, {"id": "issue-1", "stateId": "state-done"})

    def test_close_leaves_an_issue_the_agent_already_ended_alone(self) -> None:
        # A bug that could not be reproduced is canceled, not done, and the
        # outcome is the agent's to decide (FOREGENT.md).
        for state in sorted(linear.CLOSED):
            with self.subTest(state=state):
                with mock.patch.object(
                    linear, "_request", side_effect=closing(state)
                ) as request:
                    linear.close_issue("JIM-88")

                self.assertEqual(request.call_count, 1)

    def test_close_reports_a_team_with_no_done_state(self) -> None:
        reply = {"issue": {**DONE["issue"], "team": {"states": {"nodes": []}}}}
        with mock.patch.object(linear, "_request", return_value=reply):
            with self.assertRaises(linear.LinearError):
                linear.close_issue("JIM-88")


if __name__ == "__main__":
    unittest.main()
