"""Tests for the persisted issue store (JIM-249).

The store's in-memory behavior is driven through the server tests; these
cover the file: what a restart reads back, and what it does not.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foregent import store as store_module
from foregent.agents import AgentRef, Provider
from foregent.models import Issue, IssueStatus
from foregent.store import STATE_VERSION, IssueStore


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # A directory that does not exist yet, so the first save has to make it.
        self.path = self.tmp / "foregent" / "state.json"

    def reopen(self) -> IssueStore:
        """A second store on the same file: what the next boot would read."""
        return IssueStore(self.path)

    def reread(self, key: str) -> Issue:
        """Issue ``key`` as the next boot would read it."""
        issue = self.reopen().get(key)
        assert issue is not None
        return issue

    @staticmethod
    def head(store: IssueStore) -> str:
        issue = store.next_queued()
        assert issue is not None
        return issue.key

    def test_every_field_of_an_issue_survives_a_reopen(self) -> None:
        issue = Issue(
            key="JIM-88",
            title="Persist the store",
            status=IssueStatus.BLOCKED,
            repo="/src/repo",
            directory="/ws/JIM-88",
            provider=Provider.CODEX,
            model="gpt-5",
            blocker="a review of the PR",
            parent="JIM-198",
            agent=AgentRef("fg-jim-88", "conversation-1"),
        )
        IssueStore(self.path).add(issue)

        self.assertEqual(self.reopen().get("JIM-88"), issue)

    def test_a_delegated_issue_comes_back_a_sub_issue(self) -> None:
        # The parent is what admits the issue and what its completion wakes
        # (JIM-250), so a restart that forgot it would launch the child
        # against the wrong limit and tell nobody when it landed.
        IssueStore(self.path).queue("JIM-88", "/src/repo", parent="JIM-198")

        self.assertEqual(self.reread("JIM-88").parent, "JIM-198")

    def test_an_issue_the_operator_queues_has_no_parent(self) -> None:
        store = IssueStore(self.path)
        store.queue("JIM-88", "/src/repo", parent="JIM-198")

        store.queue("JIM-88", "/src/repo")

        self.assertIsNone(self.reread("JIM-88").parent)

    def test_the_queue_comes_back_in_order(self) -> None:
        # Insertion order is the queue order, and the file is a list so it
        # keeps it: a restart must not reorder who goes next.
        store = IssueStore(self.path)
        for key in ("JIM-3", "JIM-1", "JIM-2"):
            store.queue(key, "/src/repo")
        store.queue("JIM-3", "/src/repo")  # re-queued: to the back

        reopened = self.reopen()
        queued = [i.key for i in reopened.list_issues() if i.status is IssueStatus.QUEUED]
        self.assertEqual(sorted(queued), ["JIM-1", "JIM-2", "JIM-3"])
        self.assertEqual(self.head(reopened), "JIM-1")
        reopened.complete("JIM-1")
        self.assertEqual(self.head(reopened), "JIM-2")
        reopened.complete("JIM-2")
        self.assertEqual(self.head(reopened), "JIM-3")

    def test_every_mutation_is_written_through(self) -> None:
        # Each path that changes the store changes the file, so nothing can
        # move in memory and be lost at the next boot.
        store = IssueStore(self.path)
        store.queue("JIM-88", "/src/repo")
        self.assertEqual(self.reread("JIM-88").status, IssueStatus.QUEUED)
        store.add(
            Issue(
                key="JIM-88",
                title="",
                status=IssueStatus.IN_PROGRESS,
                agent=AgentRef("fg-jim-88", "c"),
            )
        )
        store.block("JIM-88", "a review")
        self.assertEqual(self.reread("JIM-88").blocker, "a review")
        store.unblock("JIM-88")
        self.assertEqual(self.reread("JIM-88").status, IssueStatus.IN_PROGRESS)
        store.orphan("JIM-88")
        self.assertEqual(self.reread("JIM-88").status, IssueStatus.ORPHANED)
        store.complete("JIM-88")
        self.assertEqual(self.reread("JIM-88").status, IssueStatus.DONE)

    def test_the_file_names_its_version(self) -> None:
        IssueStore(self.path).queue("JIM-88", "/src/repo")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], STATE_VERSION)
        self.assertEqual([i["key"] for i in data["issues"]], ["JIM-88"])

    def test_a_missing_file_starts_empty_without_a_warning(self) -> None:
        # The ordinary first boot, not a fault.
        with self.assertNoLogs(store_module.logger, "WARNING"):
            store = IssueStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertFalse(self.path.exists())

    def test_an_unreadable_file_starts_empty_and_says_so(self) -> None:
        self.path.parent.mkdir()
        for content in ("not json", '{"version": 1}', '{"version": 1, "issues": [{}]}', "[]"):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertLogs(store_module.logger, "WARNING"):
                    store = IssueStore(self.path)
                self.assertEqual(len(store), 0)

    def test_a_version_mismatch_starts_empty_and_says_so(self) -> None:
        # No migration: a file this code did not write is not half-read. The
        # fallback is the store the bridge had before there was a file, which
        # the boot reconciliation rebuilds from the live agents.
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": STATE_VERSION + 1, "issues": []}))
        with self.assertLogs(store_module.logger, "WARNING") as logs:
            store = IssueStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertIn("version", logs.output[0])

    def test_a_torn_write_leaves_the_previous_state_readable(self) -> None:
        # The snapshot goes to a sibling file and is renamed over the old one,
        # so a crash mid-write — here, fsync failing — leaves the file the
        # next boot reads exactly as it was. An in-place rewrite would leave
        # it truncated or half-written.
        store = IssueStore(self.path)
        store.queue("JIM-88", "/src/repo")
        before = self.path.read_text()

        with mock.patch.object(os, "fsync", side_effect=OSError("disk gone")):
            with self.assertLogs(store_module.logger, "ERROR"):
                store.queue("JIM-89", "/src/repo")

        self.assertEqual(self.path.read_text(), before)
        self.assertEqual([i.key for i in self.reopen()], ["JIM-88"])
        # And the store in memory carried on: the file is for the next boot.
        self.assertEqual(len(store), 2)
        # Nothing half-written is left beside it either.
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_a_store_without_a_path_writes_nothing(self) -> None:
        store = IssueStore()
        store.queue("JIM-88", "/src/repo")
        self.assertIsNone(store.path)
        self.assertEqual(list(self.tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
