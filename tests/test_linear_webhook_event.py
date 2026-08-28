"""Tests for mapping a Linear webhook delivery to an event (JIM-130).

Driven by real deliveries. ``tests/payloads/`` holds bodies ``/webhooks/linear``
logged since JIM-128, verbatim except for the ProseMirror mirrors of the
Markdown fields (``descriptionData``, ``bodyData``), which are long and which
nothing reads. Writing the mapping against a guess at the schema is the
mistake these fixtures exist to prevent: ``actor`` and ``updatedFrom`` are
top-level and not inside ``data``, ``actor`` is sometimes ``null``, and
``updatedFrom`` names a changed relation by raw id.

The mapping is pure, so none of this needs a server or a live workspace.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from foregent import linear
from foregent.events import Event, EventKind, wakes

PAYLOADS = pathlib.Path(__file__).parent / "payloads"

# The account foregent writes as, from the deliveries themselves: it created
# the comment below and made the assignee + state change on JIM-130.
FOREGENT = "442931c7-7df1-4c3f-994d-43dac8f75c3e"


def payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"linear_{name}.json").read_text())


def mapped(name: str) -> Event:
    """The event a captured delivery maps to; it is a failure if it maps to none."""
    event = linear.webhook_event(payload(name))
    assert event is not None, f"{name} mapped to nothing"
    return event


class CommentTests(unittest.TestCase):
    """A ``Comment`` delivery."""

    def setUp(self) -> None:
        self.event = mapped("comment")

    def test_a_comment_is_a_comment_on_the_issue_it_was_left_on(self) -> None:
        # The key is under `data.issue`, not `data`: the entity delivered is
        # the comment, and the issue is what it hangs off.
        self.assertEqual(self.event.kind, EventKind.COMMENT)
        self.assertEqual(self.event.issue_key, "JIM-130")

    def test_the_comment_text_is_the_body(self) -> None:
        self.assertIn("## Plan", self.event.body)

    def test_the_author_is_carried_for_the_wake_message(self) -> None:
        self.assertEqual(self.event.author, "AJ")

    def test_foregents_own_comment_is_filtered_by_actor(self) -> None:
        # An agent posts through the Linear MCP as foregent's own account, so
        # its comment arrives straight back here. Losing the actor would wake
        # the agent with its own writing, and a wake that causes a write is a
        # loop.
        self.assertEqual(self.event.actor, FOREGENT)
        self.assertEqual(wakes(self.event, viewer=FOREGENT), "")
        self.assertEqual(wakes(self.event, viewer="somebody-else"), "JIM-130")


class IssueUpdateTests(unittest.TestCase):
    """An ``Issue`` delivery with ``updatedFrom``."""

    def setUp(self) -> None:
        self.event = mapped("issue_update")

    def test_an_issue_update_is_an_update_of_the_issue_delivered(self) -> None:
        # No `data.issue` here — the issue is the entity, so the key is its
        # own `identifier`.
        self.assertEqual(self.event.kind, EventKind.ISSUE_UPDATE)
        self.assertEqual(self.event.issue_key, "JIM-130")

    def test_every_changed_field_is_reported(self) -> None:
        fields = [line.split(":")[0] for line in self.event.body.splitlines()]
        self.assertEqual(fields, ["startedAt", "assignee", "state"])

    def test_a_changed_relation_reads_as_what_it_points_at(self) -> None:
        # Linear reports the change as `stateId` and carries the state
        # itself alongside, so the agent is told a name rather than a UUID.
        self.assertIn("state: ", self.event.body)
        self.assertTrue(self.event.body.endswith(" → In Progress"))

    def test_the_previous_value_is_the_one_linear_sent(self) -> None:
        # Only the new side of a relation resolves to a name; resolving the
        # old id would take a second call, and this mapping is pure.
        self.assertIn(
            "f6a28504-0f21-49ad-b4f7-aa0940277df8 → In Progress", self.event.body
        )
        self.assertIn("assignee: none → AJ", self.event.body)

    def test_linears_own_bookkeeping_is_left_out(self) -> None:
        # Linear rewrites these on every write; reporting them would bury
        # the field a person actually changed.
        self.assertNotIn("updatedAt", self.event.body)
        self.assertNotIn("sortOrder", self.event.body)

    def test_foregents_own_claim_is_filtered_by_actor(self) -> None:
        # This delivery *is* foregent claiming JIM-130 — assignee and state in
        # one mutation. Waking on it would wake the agent the claim just
        # launched.
        self.assertEqual(self.event.actor, FOREGENT)
        self.assertEqual(wakes(self.event, viewer=FOREGENT), "")


class UnmappedTests(unittest.TestCase):
    """Deliveries that wake nobody."""

    def test_a_payload_naming_no_issue_is_dropped(self) -> None:
        # A label definition changed, which Linear reports without naming any
        # issue that carries the label. It also arrived with no actor at all.
        self.assertIsNone(linear.webhook_event(payload("issue_label")))

    def test_an_unrecognized_shape_is_dropped_rather_than_guessed_at(self) -> None:
        # Linear's inbox notifications carry `notification`, not `data`. The
        # envelope every entity delivery has is a `type` and a `data`; a
        # payload without it is not read further.
        self.assertIsNone(
            linear.webhook_event(
                {
                    "action": "issueCommentMention",
                    "type": "AppUserNotification",
                    "notification": {"issue": {"identifier": "JIM-130"}},
                }
            )
        )

    def test_an_entity_with_no_type_is_dropped(self) -> None:
        # Without the type there is no telling a comment from a field change.
        self.assertIsNone(
            linear.webhook_event({"action": "create", "data": {"identifier": "JIM-1"}})
        )

    def test_an_empty_payload_is_dropped(self) -> None:
        self.assertIsNone(linear.webhook_event({}))


class OtherEntityTests(unittest.TestCase):
    """Anything else that carries an issue."""

    def test_an_issue_create_maps_with_nothing_to_report(self) -> None:
        # No `updatedFrom` on a create, so there is no previous value to
        # show; the event still names its issue and its actor.
        event = mapped("issue_create")
        self.assertEqual(event.kind, EventKind.ISSUE_UPDATE)
        self.assertEqual(event.issue_key, "JIM-133")
        self.assertEqual(event.body, "")

    def test_an_entity_hanging_off_an_issue_maps_on_carrying_one(self) -> None:
        # Reactions, attachments and the rest are not enumerated: mapping
        # keys on naming an issue, so they arrive already handled. Shaped
        # after the `data.issue` block the captured comment carries.
        event = linear.webhook_event(
            {
                "action": "create",
                "type": "Reaction",
                "actor": {"id": "operator", "name": "Jim Posen"},
                "data": {"emoji": "+1", "issue": {"identifier": "JIM-130"}},
            }
        )
        assert event is not None
        self.assertEqual(event.kind, EventKind.ISSUE_UPDATE)
        self.assertEqual(event.issue_key, "JIM-130")
        self.assertEqual(wakes(event, viewer=FOREGENT), "JIM-130")


if __name__ == "__main__":
    unittest.main()
