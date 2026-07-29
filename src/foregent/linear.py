"""Minimal Linear GraphQL client.

Linear ships no first-party client library either, so this is a thin
wrapper, built on the ``gql`` library, over the little foregent needs of
Linear directly: claiming an issue, and asking what changed on the issues it
is tracking. This is the bridge's own direct Linear access, separate from the
agent-facing Linear MCP (docs/PLAN.md §5.12).
"""

from __future__ import annotations

import os

from gql import Client, gql
from gql.transport.exceptions import TransportError
from gql.transport.requests import RequestsHTTPTransport

from foregent.events import Event, EventKind

# Cap on each Linear call: claim_issue runs inside a foregent request
# handler, so a wedged Linear API must fail the request (502), not hang.
TIMEOUT = 30

# Comments read per tick. A window with more than this is served oldest-first
# across consecutive ticks rather than truncated; see :func:`poll_comments`.
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
# contiguous prefix and the next tick collects the remainder.
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
    account and comes straight back as a change, so the poll has to know it
    to leave its own writes alone (docs/PLAN.md §5.1).
    """
    return _request(_VIEWER_QUERY, {})["viewer"]["id"]


def poll_comments(
    keys: list[str], since: str, viewer: str = ""
) -> tuple[list[Event], str]:
    """Comments left on ``keys`` after ``since``, with the cursor to poll from next.

    One query for the whole fleet (docs/PLAN.md §5.1), keyed on the issues the
    bridge is tracking so cost scales with work in progress rather than with
    workspace size. ``keys`` are issue identifiers (``JIM-36``), which Linear
    accepts in this filter as it does in ``issue(id:)``.

    ``since`` is a cursor, not a clock: the returned one is the ``createdAt``
    of the last comment served, or ``since`` unchanged when nothing came back.
    A tick that runs late or restarts therefore resumes from what it has
    actually seen instead of assuming the interval elapsed exactly, and
    because ``gt`` is compared against a timestamp Linear itself issued, the
    comment the cursor names is never served twice.

    ``viewer`` drops foregent's own writes server-side, one step earlier than
    :func:`~foregent.events.wakes` does; pass it. An empty ``viewer`` matches
    nothing and so filters nothing — polling that way wakes agents with their
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
