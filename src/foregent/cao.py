"""Minimal cao-server REST client.

CAO ships no client library, so this is a thin stdlib-``urllib`` wrapper over
the endpoints foregent needs (decision on JIM-49: no CAO dependency). Auth is
default-off on localhost, so no headers are needed.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

# Cap on each CAO call: dispatch runs inside foregent request handlers, so a
# wedged cao-server must fail the request (502), not hang the threadpool.
TIMEOUT = 30

# Marker embedded in a dispatched supervisor's CAO session name so the
# issue<->agent map can be rebuilt from `GET /sessions` on startup
# (JIM-52). cao-server prepends its own "cao-" prefix, and tmux session
# names forbid "/", so the requested name is "foregent-<KEY>" and the real
# name becomes "cao-foregent-<KEY>".
SESSION_MARKER = "foregent-"


def session_name_for(issue_key: str) -> str:
    """CAO session name to request for ``issue_key``'s supervisor."""
    return f"{SESSION_MARKER}{issue_key}"


def issue_key_from_session(session_name: str) -> str | None:
    """Issue key embedded in ``session_name``, or ``None`` if it is not a
    foregent-dispatched session."""
    _, sep, key = session_name.partition(SESSION_MARKER)
    return key if sep and key else None


def api_url() -> str:
    """Base URL of cao-server, honoring CAO's own env vars."""
    host = os.environ.get("CAO_API_HOST", "127.0.0.1")
    port = os.environ.get("CAO_API_PORT", "9889")
    return f"http://{host}:{port}"


def _request(method: str, path: str, params: dict[str, str] | None = None) -> bytes:
    url = f"{api_url()}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def create_session(
    agent_profile: str,
    working_directory: str,
    session_name: str | None = None,
) -> dict:
    """Launch a CAO session and return its Terminal record.

    The fields foregent uses are ``id`` (terminal id) and ``session_name``.
    Equivalent to ``cao launch --agents ... --working-directory ...``.
    """
    params = {
        "agent_profile": agent_profile,
        "working_directory": working_directory,
    }
    if session_name is not None:
        params["session_name"] = session_name
    body = _request("POST", "/sessions", params)
    return json.loads(body)


def list_sessions() -> list[dict]:
    """Return cao-server's live sessions as ``{id, name, status}`` records."""
    return json.loads(_request("GET", "/sessions"))


def send_message(terminal_id: str, message: str, sender_id: str = "foregent") -> None:
    """Deliver ``message`` to terminal ``terminal_id``'s CAO inbox."""
    _request(
        "POST",
        f"/terminals/{terminal_id}/inbox/messages",
        {"sender_id": sender_id, "message": message},
    )


def delete_session(session_name: str) -> None:
    """Tear down CAO session ``session_name`` (all its terminals).

    Equivalent to ``DELETE /sessions/{session_name}``. Used to shut down a
    task_supervisor's session once its issue is complete.
    """
    _request("DELETE", f"/sessions/{urllib.parse.quote(session_name)}")
