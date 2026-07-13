"""Unit tests for the session-naming helpers in ``foregent.cao`` (JIM-52).

Pure functions, no network — this is not gated like ``test_cao_integration``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from foregent import cao, server
from foregent.models import IssueStatus


class SessionNamingTests(unittest.TestCase):
    def test_session_name_for(self) -> None:
        self.assertEqual(cao.session_name_for("JIM-52"), "foregent-JIM-52")

    def test_issue_key_from_session_with_cao_prefix(self) -> None:
        self.assertEqual(
            cao.issue_key_from_session("cao-foregent-JIM-52"), "JIM-52"
        )

    def test_issue_key_from_session_without_cao_prefix(self) -> None:
        self.assertEqual(cao.issue_key_from_session("foregent-JIM-52"), "JIM-52")

    def test_round_trip(self) -> None:
        session_name = "cao-" + cao.session_name_for("JIM-52")
        self.assertEqual(cao.issue_key_from_session(session_name), "JIM-52")

    def test_non_foregent_session_returns_none(self) -> None:
        self.assertIsNone(cao.issue_key_from_session("cao-e2dabf3c"))


class RebuildStoreTests(unittest.TestCase):
    """``server.rebuild_store`` against a canned ``cao.list_sessions``, no
    real cao-server."""

    def setUp(self) -> None:
        server.store = server.IssueStore()
        self.addCleanup(setattr, server, "store", server.IssueStore())

    def test_rebuild_store_recovers_foregent_sessions_only(self) -> None:
        sessions = [
            {"id": "cao-foregent-JIM-52", "name": "cao-foregent-JIM-52", "status": "running"},
            {"id": "cao-e2dabf3c", "name": "cao-e2dabf3c", "status": "running"},
        ]
        with mock.patch.object(cao, "list_sessions", return_value=sessions):
            server.rebuild_store()

        issues = server.store.list_issues()
        self.assertEqual([issue.key for issue in issues], ["JIM-52"])
        self.assertEqual(issues[0].status, IssueStatus.IN_PROGRESS)
        self.assertEqual(issues[0].session, "cao-foregent-JIM-52")

    def test_rebuild_store_survives_unreachable_cao_server(self) -> None:
        with mock.patch.object(cao, "list_sessions", side_effect=OSError("refused")):
            server.rebuild_store()  # must not raise
        self.assertEqual(len(server.store), 0)


if __name__ == "__main__":
    unittest.main()
