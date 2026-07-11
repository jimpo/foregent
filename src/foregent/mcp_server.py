"""The foregent MCP server.

The outward lifecycle interface the ``task_supervisor`` agent uses to signal an
issue's progress (``docs/PLAN.md`` §5.2). Like the CLI, it is a thin stdlib
HTTP client of the API server (:mod:`foregent.server`); the store lives there.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

from foregent.config import api_url

mcp = FastMCP("foregent")


@mcp.tool()
def complete_task(issue_key: str) -> str:
    """Record ``issue_key`` as Done in the foregent issue store."""
    request = urllib.request.Request(
        f"{api_url()}/issues/{issue_key}/complete", method="POST"
    )
    try:
        with urllib.request.urlopen(request):
            pass
    except urllib.error.URLError as exc:
        return f"Cannot reach foregent server at {api_url()}: {exc.reason}"
    return f"Marked {issue_key} complete."


@mcp.tool()
def report_blocked(issue_key: str, blocker: str) -> str:
    """Record ``blocker`` on ``issue_key`` in the foregent issue store.

    The agent session stays parked alive in its workspace: this only records
    state, nothing is terminated, and there is no wake mechanism here (the
    bridge posts the resolving event later; see docs/PLAN.md §5.6).
    """
    request = urllib.request.Request(
        f"{api_url()}/issues/{issue_key}/block",
        data=json.dumps({"blocker": blocker}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request):
            pass
    except urllib.error.URLError as exc:
        return f"Cannot reach foregent server at {api_url()}: {exc.reason}"
    return f"Recorded blocker {blocker!r} on {issue_key}."


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
