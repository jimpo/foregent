"""Minimal cao-server REST client.

CAO ships no client library, so this is a thin stdlib-``urllib`` wrapper over
the two endpoints foregent needs (decision on JIM-49: no CAO dependency).
Both endpoints take query parameters with an empty body. Auth is default-off
on localhost, so no headers are needed.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

# Cap on each CAO call: dispatch runs inside foregent request handlers, so a
# wedged cao-server must fail the request (502), not hang the threadpool.
TIMEOUT = 30


def api_url() -> str:
    """Base URL of cao-server, honoring CAO's own env vars."""
    host = os.environ.get("CAO_API_HOST", "127.0.0.1")
    port = os.environ.get("CAO_API_PORT", "9889")
    return f"http://{host}:{port}"


def _post(path: str, params: dict[str, str]) -> bytes:
    request = urllib.request.Request(
        f"{api_url()}{path}?{urllib.parse.urlencode(params)}",
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def create_session(agent_profile: str, working_directory: str) -> dict:
    """Launch a CAO session and return its Terminal record.

    The fields foregent uses are ``id`` (terminal id) and ``session_name``.
    Equivalent to ``cao launch --agents ... --working-directory ...``.
    """
    body = _post(
        "/sessions",
        {"agent_profile": agent_profile, "working_directory": working_directory},
    )
    return json.loads(body)


def send_message(terminal_id: str, message: str, sender_id: str = "foregent") -> None:
    """Deliver ``message`` to terminal ``terminal_id``'s CAO inbox."""
    _post(
        f"/terminals/{terminal_id}/inbox/messages",
        {"sender_id": sender_id, "message": message},
    )
