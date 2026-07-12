"""Minimal Linear GraphQL client.

Linear ships no first-party client library either, so this is a thin
wrapper, built on the ``gql`` library, over the single mutation foregent
needs: claiming an issue. This is the bridge's own direct Linear access,
separate from the agent-facing Linear MCP (docs/PLAN.md §5.12).
"""

from __future__ import annotations

import os

from gql import Client, gql
from gql.transport.exceptions import TransportError
from gql.transport.requests import RequestsHTTPTransport

# Cap on each Linear call: claim_issue runs inside a foregent request
# handler, so a wedged Linear API must fail the request (502), not hang.
TIMEOUT = 30

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
