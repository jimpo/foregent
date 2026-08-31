"""What GitHub delivers to the bridge.

The inbound half only: :func:`webhook_authentic` proves a payload came from
GitHub and :func:`webhook_event` turns it into an event in foregent's own
shape. Nothing above this module reads a GitHub payload, in the shape of
:mod:`foregent.linear` — a transport is a source feeding one matcher, so
GitHub is a second source rather than a second pipeline.

Agents reach GitHub the other way, through the GitHub MCP server the machine
is provisioned with, so the bridge needs no API client of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from foregent.events import Event, EventKind

# The header GitHub signs every webhook delivery with
# (:func:`webhook_authentic`), and the one naming what the delivery is about.
# The body says the repository and the pull request, never the event type.
SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"

# What GitHub prefixes the digest with, naming the algorithm it used.
_PREFIX = "sha256="


class GitHubError(Exception):
    """Raised when a GitHub interaction cannot be completed."""


def webhook_authentic(body: bytes, signature: str) -> bool:
    """Whether ``signature`` proves ``body`` is a delivery from GitHub.

    GitHub signs the exact bytes it sent, keyed on the webhook's secret, so
    the caller has to hand over the body it received rather than a
    re-serialization of a parse of it — a round trip through JSON moves the
    whitespace and the digest stops matching.

    Raises :class:`GitHubError` when ``GITHUB_WEBHOOK_SECRET`` is unset. A
    bridge that cannot check a signature says so; it does not accept.
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise GitHubError("GITHUB_WEBHOOK_SECRET is not set")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(_PREFIX + digest, signature)


# Linear names an agent's branch after the issue it is for
# (``aj/jim-141-deliver-github-pr-updates-to-workers``) and links a pull
# request opened from that branch back to the issue. The key is therefore in
# the branch, and reading it there *is* the link: no GitHub or Linear call is
# needed to follow it. A branch naming nothing that looks like a key belongs
# to a pull request foregent did not open.
_BRANCH_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")

# The deliveries that are a person answering an agent, and the one action of
# each that is the answer arriving. An edited or deleted review comment is a
# reviewer amending themselves, not new feedback to act on.
_REVIEW = "pull_request_review"
_REVIEW_COMMENT = "pull_request_review_comment"


def webhook_event(payload: dict, kind: str) -> Event | None:
    """The foregent event a GitHub webhook delivery is, or ``None`` for none.

    Pure, and the whole of understanding a GitHub payload: everything above
    it works in foregent's own shape, as on the Linear side.

    ``kind`` is the ``X-GitHub-Event`` header, because only the header says
    what a delivery is about. A review being submitted and a review comment
    being written are the two that map; every other event and every other
    action returns ``None``, an organization webhook carrying far more than
    foregent has any use for.

    The issue is resolved from the pull request's head branch
    (:func:`issue_key`), and the body carries what the agent needs to act on
    the feedback without going to read the pull request: the state a review
    was submitted with, the file and line an inline comment hangs off, and
    what was written.

    **A delivery whose sender is the pull request's own author is dropped.**
    The agent opened the pull request, so a review comment it writes there
    comes back here as an event about its own issue, and a wake that causes a
    write is a loop. This is the GitHub half of what ``viewer`` does for
    Linear in :func:`~foregent.events.wakes`, and it needs no account id of
    foregent's own: the payload names both sides of the comparison. The cost
    is that a person who opens a pull request by hand does not wake the agent
    by commenting on it themselves; anyone else reviewing it does.
    """
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    sender = payload.get("sender") or {}
    if sender.get("login") and sender.get("login") == (
        pull_request.get("user") or {}
    ).get("login"):
        return None
    action = payload.get("action")
    if kind == _REVIEW and action == "submitted":
        review = payload.get("review") or {}
        state = (review.get("state") or "").lower().replace("_", " ")
        body = _joined(f"Review state: {state}." if state else "", review.get("body"))
    elif kind == _REVIEW_COMMENT and action == "created":
        comment = payload.get("comment") or {}
        body = _joined(_where(comment), comment.get("body"))
    else:
        return None
    head = pull_request.get("head") or {}
    return Event(
        kind=EventKind.PR_REVIEW,
        issue_key=issue_key(head.get("ref") or ""),
        actor=sender.get("login") or "",
        repo=(payload.get("repository") or {}).get("full_name") or "",
        number=pull_request.get("number") or 0,
        author=sender.get("login") or "",
        body=body,
    )


def issue_key(branch: str) -> str:
    """The Linear issue key ``branch`` names, upper-cased, or ``""``.

    Linear's own branch names carry the key between the author's prefix and
    the title slug, lower-cased; a key written any other way in the branch is
    read the same. The caller checks the key against the issues foregent is
    tracking, so a branch whose first key-shaped word is something else
    resolves to an issue nobody is working and reaches nobody.
    """
    match = _BRANCH_KEY.search(branch)
    return match.group().upper() if match else ""


def _where(comment: dict) -> str:
    """The file and line an inline review comment hangs off, as ``path:line``."""
    path = comment.get("path") or ""
    line = comment.get("line") or comment.get("original_line") or 0
    return f"{path}:{line}" if path and line else path


def _joined(*parts: str | None) -> str:
    """``parts`` as paragraphs, dropping the ones that are empty."""
    return "\n\n".join(part for part in parts if part)
