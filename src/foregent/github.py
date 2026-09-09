"""What GitHub delivers to the bridge.

The inbound half only: :func:`webhook_authentic` proves a payload came from
GitHub and :func:`webhook_event` turns it into an event in foregent's own
shape. Nothing above this module reads a GitHub payload, in the shape of
:mod:`foregent.linear` — a transport is a source feeding one matcher, so
GitHub is a second source rather than a second pipeline.

Agents reach GitHub the other way, through the GitHub MCP server the machine
is provisioned with. The bridge's own reach into GitHub is one GET
(:func:`_head_ref`), for the one delivery whose payload is short of the branch
that resolves it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import urllib.request

from foregent.events import Event, EventKind

logger = logging.getLogger(__name__)

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

# Comment and review deliveries that are a person answering an agent. An
# edited or deleted review comment is a reviewer amending themselves, not new
# feedback to act on.
# ``issue_comment`` is a comment in the pull request's conversation tab — the
# ordinary way a reviewer says something that hangs off no line. GitHub sends
# it under the same name for a comment on a plain issue, and its payload calls
# the thing commented on an ``issue`` either way (:func:`_commented_on`).
_REVIEW = "pull_request_review"
_REVIEW_COMMENT = "pull_request_review_comment"
_CONVERSATION_COMMENT = "issue_comment"
_PULL_REQUEST = "pull_request"

# Pull request activity that changes what the agent should do or tells it
# what happened to its work. Everything else in this broad webhook stays
# noise: assignment, locking, milestones, and auto-merge bookkeeping.
_PR_UPDATE_ACTIONS = frozenset(
    {
        "closed",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
        "review_requested",
        "edited",
        "synchronize",
        "labeled",
        "unlabeled",
        "dequeued",
    }
)

# App-authored edits, review requests, and label changes are normally linkback,
# automation, or CI bookkeeping rather than a person changing the agent's work.
# Actions with immediate state or code consequences remain meaningful whoever
# GitHub attributes them to, and are not listed here.
_BOT_BOOKKEEPING_ACTIONS = frozenset(
    {"edited", "review_requested", "labeled", "unlabeled"}
)

# The same bookkeeping actions can come from an ordinary user when the agent
# authenticates with a personal access token. Its own head push is the other
# lifecycle write that comes straight back through the webhook.
_AUTHOR_LOOPABLE_ACTIONS = _BOT_BOOKKEEPING_ACTIONS | {"synchronize"}

# Where the bridge asks GitHub for a pull request, and how long it waits. A
# conversation comment is resolved inside the webhook route, so a wedged API
# has to give up rather than hold the route open.
_API = "https://api.github.com"
_TIMEOUT = 10

# The delivery that is the base moving under everyone with a pull request
# open, and the ref it has to name to be that. GitHub sends nothing when a
# pull request stops merging cleanly, so a push to the trunk is the only
# signal outside a merge queue — and the same one that says a pull request
# landed.
_PUSH = "push"
_TRUNK_REF = "refs/heads/main"

# How many pushed commit subjects to carry. Enough to recognize your own pull
# request landing in a batch of merges; short enough not to bury the prompt
# under a force-push of somebody's whole branch history.
_SUBJECTS = 10


def webhook_event(payload: dict, kind: str) -> Event | None:
    """The foregent event a GitHub webhook delivery is, or ``None`` for none.

    The whole of understanding a GitHub payload: everything above it works in
    foregent's own shape, as on the Linear side.

    ``kind`` is the ``X-GitHub-Event`` header, because only the header says
    what a delivery is about. A review being submitted, a comment being
    written — inline or in the conversation tab — selected pull request
    lifecycle changes, and a push to ``main`` are the ones that map; every
    other event and action returns ``None``, an organization webhook carrying
    far more than foregent has any use for.

    A push is the odd one and is handled first, because it is the one
    delivery here that carries no pull request at all (:func:`_pushed`).

    The issue is resolved from the pull request's head branch
    (:func:`issue_key`), and the body carries what the agent needs to act on
    the delivery without going to read the pull request: review state and
    comments, or a concise lifecycle description including merge state,
    reviewer, edited fields, head change, label, or dequeue reason.

    **Not pure for a conversation comment**, and that one kind alone: its
    payload names no branch, so the branch is fetched (:func:`_head_ref`).
    The caller therefore has to keep this off the event loop. Resolving it a
    level up would put the reading of a GitHub payload into the route, which
    is the boundary this module exists to hold.

    **Loop-capable deliveries from the pull request's author are dropped.**
    The agent opened the pull request, so review feedback it writes and the
    ``synchronize`` caused by its own push come back as events about its own
    issue. Lifecycle changes that cannot loop are still delivered: GitHub can
    attribute an automatic merge or another terminal change to the account that
    opened the pull request, especially when a box uses one token for both
    roles.

    App-authored edits, review requests, and label changes are dropped as
    bookkeeping. Other lifecycle changes are delivered regardless of sender
    type. The remaining cost is unchanged: if a person opens a pull request by
    hand on the agent's branch, feedback they write on that pull request is
    attributed to its author and reaches nobody; feedback from anyone else
    does.
    """
    if kind == _PUSH:
        return _pushed(payload)
    pull_request = _commented_on(payload, kind)
    if pull_request is None:
        return None
    sender = payload.get("sender") or {}
    action = payload.get("action")
    sent_by_author = sender.get("login") and sender.get("login") == (
        pull_request.get("user") or {}
    ).get("login")
    event_kind = EventKind.PR_REVIEW
    if kind == _PULL_REQUEST:
        if not isinstance(action, str) or action not in _PR_UPDATE_ACTIONS:
            return None
        if sent_by_author and action in _AUTHOR_LOOPABLE_ACTIONS:
            return None
        if (
            sender.get("type") == "Bot"
            and action in _BOT_BOOKKEEPING_ACTIONS
        ):
            return None
        event_kind = EventKind.PR_UPDATE
        body = _pull_request_update_body(payload, pull_request, action)
    elif sent_by_author:
        return None
    elif kind == _REVIEW and action == "submitted":
        review = payload.get("review") or {}
        state = (review.get("state") or "").lower().replace("_", " ")
        body = _joined(f"Review state: {state}." if state else "", review.get("body"))
    elif kind == _REVIEW_COMMENT and action == "created":
        comment = payload.get("comment") or {}
        body = _joined(_where(comment), comment.get("body"))
    elif kind == _CONVERSATION_COMMENT and action == "created":
        body = (payload.get("comment") or {}).get("body") or ""
    else:
        return None
    repo = (payload.get("repository") or {}).get("full_name") or ""
    number = pull_request.get("number") or 0
    # Everything cheap that could drop this delivery has run, so the one kind
    # that costs a GitHub call makes it here and nowhere earlier.
    branch = (
        _head_ref(repo, number)
        if kind == _CONVERSATION_COMMENT
        else (pull_request.get("head") or {}).get("ref") or ""
    )
    return Event(
        kind=event_kind,
        issue_key=issue_key(branch),
        actor=sender.get("login") or "",
        repo=repo,
        number=number,
        author=sender.get("login") or "",
        body=body,
    )


def _pull_request_update_body(payload: dict, pull_request: dict, action: str) -> str:
    """Action-specific detail worth carrying with a pull request update."""
    if action == "closed":
        return "Merged." if pull_request.get("merged") else "Closed without merging."
    if action == "reopened":
        return "Reopened."
    if action == "ready_for_review":
        return "Marked ready for review."
    if action == "converted_to_draft":
        return "Converted to draft."
    if action == "review_requested":
        reviewer = payload.get("requested_reviewer") or {}
        team = payload.get("requested_team") or {}
        name = reviewer.get("login") or team.get("name") or team.get("slug") or ""
        return f"Review requested from {name}." if name else "Review requested."
    if action == "edited":
        return _edited_fields(payload.get("changes"), pull_request)
    if action == "synchronize":
        return _values("Head updated", payload.get("before"), payload.get("after"))
    if action == "labeled":
        label = payload.get("label") or {}
        name = label.get("name") or ""
        return f"Label added: {name}." if name else "Label added."
    if action == "unlabeled":
        label = payload.get("label") or {}
        name = label.get("name") or ""
        return f"Label removed: {name}." if name else "Label removed."
    if action == "dequeued":
        reason = payload.get("reason") or ""
        suffix = f" Reason: {reason}." if reason else ""
        return f"Removed from the merge queue.{suffix}"
    return ""


def _edited_fields(changes: object, pull_request: dict) -> str:
    """Changed title, body, and base branch as old-to-new lines."""
    if not isinstance(changes, dict):
        return ""
    lines = []
    title = changes.get("title")
    if isinstance(title, dict) and "from" in title:
        lines.append(_values("Title", title.get("from"), pull_request.get("title")))
    body = changes.get("body")
    if isinstance(body, dict) and "from" in body:
        # A pull request body is arbitrary multiline Markdown. Carrying both
        # versions can bury every other change in the delivery.
        lines.append("Body edited.")
    base = changes.get("base")
    ref = base.get("ref") if isinstance(base, dict) else None
    current_base = pull_request.get("base") or {}
    if isinstance(ref, dict) and "from" in ref:
        lines.append(_values("Base", ref.get("from"), current_base.get("ref")))
    return "\n".join(lines)


def _values(label: str, before: object, after: object) -> str:
    """Render an old and new value without turning JSON null into ``None``."""
    old = "none" if before is None else str(before)
    new = "none" if after is None else str(after)
    return f"{label}: {old} → {new}"


def _commented_on(payload: dict, kind: str) -> dict | None:
    """The pull request ``payload`` is about, or ``None`` if it is about none.

    A review and a review comment name it ``pull_request``. A conversation
    comment names it ``issue``, because GitHub delivers comments on issues and
    on pull requests under one event and one payload shape; the ``pull_request``
    link inside that ``issue`` is what separates the two, and foregent opens no
    issues, so a comment on a plain one is about nothing here.

    Either way the answer carries the number and the author the rest of
    :func:`webhook_event` reads.
    """
    if kind != _CONVERSATION_COMMENT:
        pull_request = payload.get("pull_request")
        return pull_request if isinstance(pull_request, dict) else None
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not issue.get("pull_request"):
        return None
    return issue


def _head_ref(repo: str, number: int) -> str:
    """The head branch of pull request ``number`` in ``repo``, or ``""``.

    The bridge's only outbound GitHub call, for the only delivery whose
    payload leaves out the branch that resolves it to an issue. Authenticated
    with ``GITHUB_TOKEN``, which the box already holds for the agents' MCP
    server.

    **Every failure answers ``""``**, which resolves to no issue and reaches
    nobody — what the delivery did before it was handled at all. A missing
    token is an operator's misconfiguration and an unreachable API is nobody's,
    and neither is worth failing a webhook GitHub would only retry into the
    same wall, so both are logged and the comment is dropped.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "GITHUB_TOKEN is not set, so %s#%s resolves to no issue", repo, number
        )
        return ""
    request = urllib.request.Request(
        f"{_API}/repos/{repo}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        logger.warning("cannot read %s#%s from GitHub: %s", repo, number, exc)
        return ""
    if not isinstance(payload, dict):
        return ""
    return (payload.get("head") or {}).get("ref") or ""


def _pushed(payload: dict) -> Event | None:
    """The ``MAIN_ADVANCED`` event a ``push`` delivery is, or ``None``.

    **Only a push that leaves commits on ``main`` counts.** Agents push their
    own branches constantly and none of that moves anybody's base, so the ref
    is checked; a branch being deleted names a ref too, and leaves nothing to
    have advanced.

    The event names a repository and no issue: a push says the base moved, and
    which agents that matters to is a question about the issues foregent is
    working rather than about the payload
    (:func:`~foregent.events.wakes`). It carries the pushed commit subjects,
    which is what lets an agent recognize its own pull request landing without
    going to read the repository.

    **No ``actor``**, so nothing about this is dropped as foregent's own. An
    agent pushes its own branch and never ``main``, so this is not a delivery
    foregent can cause; the pusher is carried as the ``author`` the bridge
    logs the wake against, and compared to nothing.
    """
    if payload.get("ref") != _TRUNK_REF or payload.get("deleted"):
        return None
    commits = payload.get("commits")
    subjects = [
        f"- {(commit.get('message') or '').splitlines()[0]}"
        for commit in (commits if isinstance(commits, list) else [])
        if isinstance(commit, dict) and (commit.get("message") or "").strip()
    ]
    return Event(
        kind=EventKind.MAIN_ADVANCED,
        repo=(payload.get("repository") or {}).get("full_name") or "",
        author=(payload.get("sender") or {}).get("login") or "",
        body="\n".join(subjects[:_SUBJECTS]),
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
