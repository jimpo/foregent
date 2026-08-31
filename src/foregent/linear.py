"""Minimal Linear client.

Linear ships no first-party client library either, so this is a thin
wrapper, built on the ``gql`` library, over the little foregent needs of
Linear directly: claiming an issue, and asking what changed on the issues it
is tracking. This is the bridge's own direct Linear access, separate from the
agent-facing Linear MCP.

Traffic in the other direction — deliveries Linear makes to the bridge — is
here too: :func:`webhook_authentic` proves a payload came from Linear,
:func:`webhook_fresh` proves it came from Linear just now, and
:func:`webhook_event` turns it into an event in foregent's own shape. Those
three are the whole of understanding a Linear payload; nothing above this
module reads one.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from gql import Client, gql
from gql.transport.exceptions import TransportError
from gql.transport.requests import RequestsHTTPTransport

from foregent.events import Event, EventKind

# Cap on each Linear call: claim_issue runs inside a foregent request
# handler, so a wedged Linear API must fail the request (502), not hang.
TIMEOUT = 30

# The header Linear signs every webhook delivery with (:func:`webhook_authentic`).
SIGNATURE_HEADER = "Linear-Signature"

# How far from now a delivery may say it was sent and still be acted on
# (:func:`webhook_fresh`), in seconds. Linear's own replay guidance.
WINDOW = 60

# Comments read per call. A window with more than this is served oldest-first
# across consecutive calls rather than truncated; see :func:`poll_comments`.
PAGE = 50

_CLAIM_QUERY = """
query Claim($key: String!) {
  viewer { id }
  issue(id: $key) {
    id
    team {
      states(filter: { name: { eq: "In Progress" } }) {
        nodes { id }
      }
    }
  }
}
"""

_CLAIM_MUTATION = """
mutation Claim($id: String!, $assigneeId: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { assigneeId: $assigneeId, stateId: $stateId }) {
    success
  }
}
"""


_VIEWER_QUERY = """
query Viewer {
  viewer { id }
}
"""

# `last`, not `first`, and the reason is ordering: Linear returns comments
# newest-first, so `first` would serve the *newest* page of the window and a
# cursor advanced to it would skip everything older that did not fit. `last`
# serves the oldest page, ascending, so the cursor advances to the end of a
# contiguous prefix and the next call collects the remainder.
_POLL_QUERY = f"""
query Poll($since: DateTimeOrDuration!, $keys: [ID!], $viewer: ID) {{
  comments(
    filter: {{
      issue: {{ id: {{ in: $keys }} }}
      createdAt: {{ gt: $since }}
      user: {{ id: {{ neq: $viewer }} }}
    }}
    last: {PAGE}
  ) {{
    nodes {{
      id
      createdAt
      body
      user {{ id name }}
      issue {{ identifier }}
    }}
  }}
}}
"""


class LinearError(Exception):
    """Raised when a Linear API call cannot be completed."""


def api_url() -> str:
    """Base URL of the Linear GraphQL API."""
    return os.environ.get("LINEAR_API_URL", "https://api.linear.app/graphql")


def _request(query: str, variables: dict) -> dict:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise LinearError("LINEAR_API_KEY is not set")
    transport = RequestsHTTPTransport(
        url=api_url(),
        headers={"Authorization": api_key},
        timeout=TIMEOUT,
    )
    client = Client(transport=transport)
    try:
        data = client.execute(gql(query), variable_values=variables)
    except (TransportError, OSError) as exc:
        raise LinearError(f"Linear API request failed: {exc}") from exc
    if not data:
        raise LinearError("Linear API returned no data")
    return data


def claim_issue(key: str) -> None:
    """Assign issue ``key`` to the foregent account and move it to In Progress.

    Resolves the viewer id, issue id, and In Progress state id in one query,
    then performs the assignee + state change in a single ``issueUpdate``
    mutation, so the claim happens atomically in Linear.
    """
    data = _request(_CLAIM_QUERY, {"key": key})
    issue = data["issue"]
    if issue is None:
        raise LinearError(f"Linear issue {key!r} not found")
    state_nodes = issue["team"]["states"]["nodes"]
    if not state_nodes:
        raise LinearError(
            f"Linear team for {key!r} has no 'In Progress' state "
            "(provisioning precondition)"
        )
    result = _request(
        _CLAIM_MUTATION,
        {
            "id": issue["id"],
            "assigneeId": data["viewer"]["id"],
            "stateId": state_nodes[0]["id"],
        },
    )
    if not result["issueUpdate"]["success"]:
        raise LinearError(f"Linear issueUpdate for {key!r} did not succeed")


def viewer_id() -> str:
    """The Linear account id foregent itself writes as.

    Everything the bridge does to Linear — claiming an issue, and every
    comment an agent posts through the Linear MCP — is written as this
    account and comes straight back as a delivery, so the bridge has to know
    it to leave its own writes alone.
    """
    return _request(_VIEWER_QUERY, {})["viewer"]["id"]


def webhook_authentic(body: bytes, signature: str) -> bool:
    """Whether ``signature`` proves ``body`` is a delivery from Linear.

    Linear signs the exact bytes it sent, keyed on the webhook's signing
    secret, so the caller has to hand over the body it received rather than a
    re-serialization of a parse of it — a round trip through JSON moves the
    whitespace and the digest stops matching.

    Raises :class:`LinearError` when ``LINEAR_WEBHOOK_SECRET`` is unset. A
    bridge that cannot check a signature says so; it does not accept.
    """
    secret = os.environ.get("LINEAR_WEBHOOK_SECRET")
    if not secret:
        raise LinearError("LINEAR_WEBHOOK_SECRET is not set")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def webhook_fresh(payload: dict) -> bool:
    """Whether the delivery ``payload`` holds was sent within :data:`WINDOW`.

    Linear stamps every delivery with the millisecond it sent it, and asks
    that one further from now than a minute be refused. That is what makes a
    signature mean *now*: on its own it proves only that Linear sent these
    bytes at some point, so a captured delivery replays forever.

    A payload carrying no ``webhookTimestamp`` is fresh. The signature covers
    the exact bytes, so a body without the field is one Linear sent that way,
    not one stripped of it on the way here.
    """
    sent = payload.get("webhookTimestamp")
    if not isinstance(sent, (int, float)):
        return True
    return abs(time.time() - sent / 1000) <= WINDOW


def poll_comments(
    keys: list[str], since: str, viewer: str = ""
) -> tuple[list[Event], str]:
    """Comments left on ``keys`` after ``since``, with the cursor to read from next.

    **Library code with no caller.** Webhooks are how a comment reaches an
    agent; this is the catch-up read a reconciliation would use to find what
    a deliver-once path missed — a restart, an outage, a delivery Linear gave
    up retrying. It is deliberately outside the startup path, so adding that
    reconciliation is a matter of calling this, not of writing it.

    One query for the whole fleet, keyed on the issues the
    bridge is tracking so cost scales with work in progress rather than with
    workspace size. ``keys`` are issue identifiers (``JIM-36``), which Linear
    accepts in this filter as it does in ``issue(id:)``.

    ``since`` is a cursor, not a clock: the returned one is the ``createdAt``
    of the last comment served, or ``since`` unchanged when nothing came back.
    A caller that runs late or restarts therefore resumes from what it has
    actually seen instead of assuming an interval elapsed exactly, and
    because ``gt`` is compared against a timestamp Linear itself issued, the
    comment the cursor names is never served twice.

    ``viewer`` drops foregent's own writes server-side, one step earlier than
    :func:`~foregent.events.wakes` does; pass it. An empty ``viewer`` matches
    nothing and so filters nothing — reading that way wakes agents with their
    own writes, and a wake that causes a write is a loop.
    """
    if not keys:
        return [], since
    data = _request(
        _POLL_QUERY, {"since": since, "keys": keys, "viewer": viewer or None}
    )
    nodes = data["comments"]["nodes"]
    events = [
        Event(
            kind=EventKind.COMMENT,
            issue_key=node["issue"]["identifier"],
            # Linear attributes automated comments to no user at all; such an
            # event is nobody's own write, which is what `wakes` reads "" as.
            actor=(node["user"] or {}).get("id", ""),
            author=(node["user"] or {}).get("name", ""),
            body=node["body"] or "",
        )
        for node in nodes
    ]
    return events, nodes[-1]["createdAt"] if nodes else since


# What Linear rewrites on every issue write. Reporting these alongside the
# field a person actually changed buries it.
_BOOKKEEPING = frozenset({"updatedAt", "sortOrder", "prioritySortOrder"})


def _readable(value: object) -> str:
    """A value out of a Linear payload, as a person would read it.

    Linear spells a relation as a nested object with a ``name``, and a set of
    them as a list of those; everything else is a scalar it already renders.
    """
    if isinstance(value, dict):
        return str(value.get("name", value))
    if isinstance(value, list):
        return ", ".join(_readable(item) for item in value) or "none"
    return "none" if value is None else str(value)


def _current(data: dict, field: str) -> tuple[str, object]:
    """What ``field`` is called and what it now holds, after the change.

    A changed relation is reported under an id field (``stateId``) with the
    relation itself carried alongside under its own name (``state``), so the
    id is reported as the thing it points at. The previous value stays the
    raw id Linear sent: resolving it would need a second call, and this
    mapping is pure.
    """
    for suffix, plural in (("Ids", "s"), ("Id", "")):
        if field.endswith(suffix):
            name = field[: -len(suffix)] + plural
            if name in data:
                return name, data[name]
    return field, data.get(field)


def _changes(data: dict, updated_from: dict) -> str:
    """The changed fields, each with the value it held before.

    Enough for a worker to act on the update without re-reading the issue,
    which is the whole reason the body is filled in at all.
    """
    lines = []
    for field, was in updated_from.items():
        if field in _BOOKKEEPING:
            continue
        name, now = _current(data, field)
        lines.append(f"{name}: {_readable(was)} → {_readable(now)}")
    return "\n".join(lines)


def webhook_event(payload: dict) -> Event | None:
    """The foregent event a Linear webhook delivery is, or ``None`` for none.

    Pure, and the whole of understanding a Linear payload: everything above
    it works in foregent's own shape.

    A delivery maps when it is an entity delivery — a ``type`` naming the
    entity and a ``data`` object holding it — that names an issue, at
    ``data.issue.identifier`` for something attached to an issue or
    ``data.identifier`` for the issue itself. Anything else returns ``None``:
    a payload naming no issue can wake nobody, and a shape this does not
    recognize is dropped rather than guessed at.

    A comment is its text. Everything else that carries an issue is a field
    update, keyed on carrying one rather than on a list of entity types, so
    the reactions, labels and attachments Linear hangs off an issue map
    without an entry each.

    The actor is carried through because :func:`~foregent.events.wakes` drops
    foregent's own writes by identity, and this path is how they come back:
    every comment an agent posts through the Linear MCP, and the assignee and
    state change the bridge makes to claim an issue. A mapping that
    lost the actor would turn every claim into a wake of the agent it just
    launched.
    """
    data = payload.get("data")
    if not isinstance(payload.get("type"), str) or not isinstance(data, dict):
        return None
    issue = data.get("issue")
    key = (issue.get("identifier") if isinstance(issue, dict) else None) or data.get(
        "identifier"
    )
    if not isinstance(key, str) or not key:
        return None
    # Linear sends no actor for a change it made itself; such an event is
    # nobody's own write, which is what `wakes` reads "" as.
    actor = payload.get("actor") or {}
    if payload["type"] == "Comment":
        kind, body = EventKind.COMMENT, data.get("body") or ""
    else:
        kind = EventKind.ISSUE_UPDATE
        body = _changes(data, payload.get("updatedFrom") or {})
    return Event(
        kind=kind,
        issue_key=key,
        actor=actor.get("id") or "",
        author=actor.get("name") or "",
        body=body,
    )
