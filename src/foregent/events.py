"""Which issue does an incoming event belong to?

This module is the decision of *which* agent a given event was for, plus the
vocabulary it needs: a normalized :class:`Event` and a pure :func:`wakes`.

The rule is **an event goes to the agent that owns the issue the event is
about** — a comment on the issue, a change to one of its fields, or activity
on the pull request linked to it. It goes there whether that agent is working
or parked on a block: a worker should see activity on
its own issue as soon as it happens. The blocker a parked agent reported is
never read: it says what the agent was waiting for, in whatever words it
chose, and is for the operator reading ``foregent status``.

Ingestion — the periodic tick that asks Linear and GitHub what changed, and
resolving a pull request back to the Linear issue it is linked to — lands
separately. Keeping this pure is what lets it be tested without a
server, a transport, or a live agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    """The kinds of outside event an agent is told about.

    Anything else ingestion receives is not delivered. A Linear field update
    is one of them because a person answers an agent by moving the issue at
    least as often as by writing to it: a design parked for review is
    approved by a state change, and an issue cancelled under a working agent
    is something it has to be told. Which updates are worth delivering is the
    dispatcher's judgment, not this vocabulary's.
    """

    # Someone commented, or replied to a comment, on the Linear issue.
    COMMENT = "comment"
    # A field of the Linear issue changed: its state, assignee, labels.
    ISSUE_UPDATE = "issue_update"
    # A review or a comment on the linked pull request, inline or PR-level.
    PR_REVIEW = "pr_review"
    # The linked pull request stopped merging cleanly as main advanced.
    PR_CONFLICT = "pr_conflict"


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened in Linear or GitHub, normalized.

    ``issue_key`` is the Linear issue the event is *about*, and is the whole
    of matching. For GitHub events ingestion resolves it from the pull
    request's link to the issue — which Linear makes for itself off the branch
    name — so a worker never has to report its own PR number to be findable.
    An event ingestion could not attribute to an issue carries no key and
    reaches nobody.

    Deliberately flat: a per-platform payload hierarchy would put the work of
    understanding two payload formats into every consumer instead of into
    ingestion alone. The fields matching does not read (``repo``, ``number``,
    ``author``, ``body``) are what :func:`delivery_message` hands the agent,
    so it can act on the event rather than go re-read the issue to find out
    what happened.
    """

    kind: EventKind
    issue_key: str = ""
    # Whoever caused it, as the source platform identifies them. Compared
    # against foregent's own id to drop the bridge's own writes; see
    # :func:`wakes`.
    actor: str = ""
    # GitHub repository and pull request number, for the PR kinds.
    repo: str = ""
    number: int = 0
    # Human-readable name of the actor, and what they said.
    author: str = ""
    body: str = ""


def wakes(event: Event, viewer: str = "") -> str:
    """The issue whose agent ``event`` goes to, or ``""`` for none.

    Pure, and a lookup rather than a scan: the event names its own issue, so
    there is nothing to search agents for. The caller checks that the issue
    has a live agent to deliver to — an event on an issue nobody is working
    is not an error, just a message with nowhere to go.

    ``viewer`` is foregent's own account id on the event's platform. Foregent
    writes to Linear as that account on every dispatch (assignee + In
    Progress), and so does every agent posting through the Linear MCP, so
    those writes come straight back as events; without dropping them the
    bridge prompts agents with their own writes, and a prompt that triggers
    another write is a loop. The filter is on **actor identity, not
    content**. An event with no actor is never foregent's own.
    """
    if event.actor and event.actor == viewer:
        return ""
    return event.issue_key


def delivery_message(event: Event, *, parked: bool) -> str:
    """What to prompt the agent ``event`` reached with.

    Carries the event itself rather than a bare "something happened": the
    agent has to act on the feedback, and re-reading the issue to find out
    what it was costs a round trip it does not need.

    ``parked`` is the one difference between the two readers. A parked agent
    is idle and waiting for exactly this, so the message says it is being
    woken; a working agent was never waiting, and telling it that it is being
    woken is a lie it would have to reason past. The event reads the same
    either way.
    """
    who = event.author or "someone"
    pull_request = f"{event.repo}#{event.number}"
    match event.kind:
        case EventKind.COMMENT:
            what = f"{who} commented on {event.issue_key}."
        case EventKind.ISSUE_UPDATE:
            what = f"{who} updated {event.issue_key}."
        case EventKind.PR_REVIEW:
            what = f"{who} reviewed {pull_request}."
        case EventKind.PR_CONFLICT:
            what = f"{pull_request} no longer merges cleanly into main."
    header = f"Waking you: {what}" if parked else what
    return f"{header}\n\n{event.body}" if event.body else header
