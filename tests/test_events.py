"""Tests for deciding which parked agent an event wakes (JIM-101).

Matching is a pure function over the event (``docs/PLAN.md`` §5.6), so none of
this needs a server, a transport, or a live agent — which is the point of
keeping it pure.
"""

from __future__ import annotations

import unittest

from foregent.events import Event, EventKind, wake_message, wakes

# Foregent's own Linear account: it writes as this on every dispatch, and so
# does every agent posting through the Linear MCP.
FOREGENT = "viewer-id"


def comment(key: str, actor: str = "operator", body: str = "") -> Event:
    return Event(
        kind=EventKind.COMMENT, issue_key=key, actor=actor, author="AJ", body=body
    )


class WakesTests(unittest.TestCase):
    """An event wakes the agent that owns the issue it is about."""

    def test_a_comment_wakes_the_issue_it_was_left_on(self) -> None:
        self.assertEqual(wakes(comment("JIM-42")), "JIM-42")

    def test_a_comment_wakes_no_other_issue(self) -> None:
        # The whole of matching is the key, so this is the same statement as
        # above — worth pinning anyway, since it is the thing that must not
        # regress into a broadcast.
        self.assertNotEqual(wakes(comment("JIM-42")), "JIM-99")

    def test_a_review_wakes_the_issue_its_pull_request_is_linked_to(self) -> None:
        # Ingestion resolves the PR back to its issue, which Linear links for
        # itself off the branch name; the worker never reports its own PR.
        self.assertEqual(
            wakes(
                Event(
                    kind=EventKind.PR_REVIEW,
                    issue_key="JIM-42",
                    actor="reviewer",
                    repo="jimpo/binius64",
                    number=123,
                )
            ),
            "JIM-42",
        )

    def test_a_conflict_with_main_wakes_the_issue_too(self) -> None:
        self.assertEqual(
            wakes(
                Event(
                    kind=EventKind.PR_CONFLICT,
                    issue_key="JIM-42",
                    repo="jimpo/binius64",
                    number=123,
                )
            ),
            "JIM-42",
        )

    def test_an_event_attributed_to_no_issue_wakes_nobody(self) -> None:
        # A pull request ingestion could not link back to a Linear issue.
        self.assertEqual(
            wakes(Event(kind=EventKind.PR_REVIEW, repo="binius64", number=7)), ""
        )


class SelfEventTests(unittest.TestCase):
    """Foregent must not wake agents with its own writes."""

    def test_foregents_own_comment_wakes_nobody(self) -> None:
        # Every dispatch writes to Linear as this account, and so does every
        # agent posting through the Linear MCP. A wake that triggers another
        # write is a loop.
        self.assertEqual(wakes(comment("JIM-42", actor=FOREGENT), FOREGENT), "")

    def test_someone_elses_comment_still_wakes(self) -> None:
        self.assertEqual(wakes(comment("JIM-42", actor="operator"), FOREGENT), "JIM-42")

    def test_an_event_with_no_actor_is_not_foregents(self) -> None:
        self.assertEqual(wakes(comment("JIM-42", actor=""), FOREGENT), "JIM-42")


class WakeMessageTests(unittest.TestCase):
    """What the woken agent is told (docs/PLAN.md §5.6)."""

    def test_a_comment_carries_who_said_what(self) -> None:
        # Not merely "you are unblocked": the agent has to act on the
        # feedback, and re-reading the issue is a round trip it does not need.
        message = wake_message(comment("JIM-42", body="the schema is wrong"))
        self.assertIn("AJ", message)
        self.assertIn("JIM-42", message)
        self.assertIn("the schema is wrong", message)

    def test_an_issue_update_carries_what_changed(self) -> None:
        # A state change is an answer as much as a comment is, so the agent
        # is told which field moved rather than that "something happened".
        message = wake_message(
            Event(
                kind=EventKind.ISSUE_UPDATE,
                issue_key="JIM-42",
                actor="operator",
                author="AJ",
                body="state: Todo → Cancelled",
            )
        )
        self.assertIn("AJ", message)
        self.assertIn("JIM-42", message)
        self.assertIn("state: Todo → Cancelled", message)

    def test_a_review_names_the_pull_request(self) -> None:
        message = wake_message(
            Event(
                kind=EventKind.PR_REVIEW,
                issue_key="JIM-42",
                repo="jimpo/binius64",
                number=123,
                author="AJ",
                body="rename this",
            )
        )
        self.assertIn("AJ", message)
        self.assertIn("jimpo/binius64#123", message)
        self.assertIn("rename this", message)

    def test_a_conflict_says_what_broke(self) -> None:
        # Nobody said anything here, so the message has to stand on its own.
        message = wake_message(
            Event(
                kind=EventKind.PR_CONFLICT,
                issue_key="JIM-42",
                repo="jimpo/binius64",
                number=123,
            )
        )
        self.assertIn("jimpo/binius64#123", message)
        self.assertIn("main", message)

    def test_an_anonymous_comment_still_reads(self) -> None:
        message = wake_message(Event(kind=EventKind.COMMENT, issue_key="BIN-7"))
        self.assertIn("someone", message)
        self.assertIn("BIN-7", message)


if __name__ == "__main__":
    unittest.main()
