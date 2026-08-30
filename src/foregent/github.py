"""What GitHub delivers to the bridge.

The inbound half only: :func:`webhook_authentic` proves a payload came from
GitHub. Nothing above this module reads a GitHub payload, in the shape of
:mod:`foregent.linear` — a transport is a source feeding one matcher, so
GitHub is a second source rather than a second pipeline.

Agents reach GitHub the other way, through the GitHub MCP server the machine
is provisioned with, so the bridge needs no API client of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import os

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
