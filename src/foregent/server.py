"""The foregent API server.

Owns the authoritative :class:`~foregent.store.IssueStore` and exposes it over
HTTP so the CLI can stay a thin client (``docs/PLAN.md`` §2, Bridge core). In
this skeleton the store starts empty and is never populated — the rebuild path
lands with the bridge.
"""

from __future__ import annotations

from fastapi import FastAPI

from foregent.store import IssueStore

app = FastAPI(title="foregent")

# The single, process-wide issue store this server serves. Empty in the
# skeleton; rebuilt from CAO + Linear once the bridge lands.
store = IssueStore()


@app.get("/issues")
def list_issues() -> list[dict[str, str]]:
    """Return the tracked issues as ``{key, title, status}`` records."""
    return [
        {"key": issue.key, "title": issue.title, "status": issue.status}
        for issue in store.list_issues()
    ]


@app.post("/issues/{key}/complete")
def complete_issue(key: str) -> dict[str, str]:
    """Mark issue ``key`` Done and return the resulting record."""
    issue = store.complete(key)
    return {"key": issue.key, "title": issue.title, "status": issue.status}
