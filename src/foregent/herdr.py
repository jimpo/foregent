"""Minimal herdr socket API client.

herdr ships no client library, so this is a thin stdlib-``socket`` wrapper
over the newline-delimited JSON API foregent drives (``docs/PLAN.md`` §2,
§4). Access control is unix-socket file permissions, so there is nothing to
authenticate.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path

# Protocol version this client is written against. herdr is a hard
# dependency (docs/PLAN.md §7), so a drift here must stop the bridge at
# startup rather than surface as mystery errors mid-dispatch.
PROTOCOL = 17

# Default per-call budget. Calls that block server-side (``agent.wait``,
# ``agent.prompt`` with ``wait``) must pass their own, larger timeout — see
# `timeout_for_wait`.
TIMEOUT = 30

# Slack added on top of a server-side wait budget when deriving the socket
# timeout, so herdr's own timeout wins the race and the caller sees a real
# error response instead of a severed connection.
TIMEOUT_MARGIN = 10

# herdr keeps its sockets under a fixed config directory; the default
# session sits at the root and named sessions get a subdirectory each.
CONFIG_DIR = Path.home() / ".config" / "herdr"


class HerdrError(Exception):
    """Base for every failure talking to herdr."""


class HerdrAPIError(HerdrError):
    """herdr answered with an ``error`` response.

    ``code`` is herdr's machine-readable code (e.g. ``agent_prompt_stalled``,
    ``invalid_request``), which callers switch on — the stalled-prompt case
    in particular is recoverable and must be distinguishable from a genuine
    transport failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HerdrTransportError(HerdrError):
    """The socket was unreachable, or its answer was unreadable."""


def socket_path(session: str | None = None) -> str:
    """Path of the herdr API socket to talk to.

    Resolution mirrors herdr's own precedence: an explicit session name wins,
    then ``HERDR_SOCKET_PATH``, then ``HERDR_SESSION``, then the default
    session. The env vars matter because herdr injects them into every pane
    it owns, so an agent-side caller inherits the right socket for free.

    A ``HERDR_SOCKET_PATH`` pointing at a dead socket is returned as-is rather
    than falling back: connecting then fails naming that path, which says what
    is wrong, where quietly talking to a *different* session than the
    environment claims would not.
    """
    if session is None:
        explicit = os.environ.get("HERDR_SOCKET_PATH")
        if explicit:
            return explicit
        session = os.environ.get("HERDR_SESSION", "")
    if not session or session == "default":
        return str(CONFIG_DIR / "herdr.sock")
    return str(CONFIG_DIR / "sessions" / session / "herdr.sock")


def describe_session(session: str | None, path: str) -> str:
    """One line naming the session and socket a client resolved to.

    Which herdr foregent talks to depends on how its process was started, so
    startup says which session it landed on outright — otherwise an operator
    infers it from a socket path in whatever error comes later.
    """
    if session:
        return f"herdr session {session!r} at {path}"
    if os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HERDR_SESSION"):
        return f"the herdr session this process runs in, at {path}"
    return f"herdr's default session at {path}"


def timeout_for_wait(timeout_ms: int | None) -> float:
    """Socket budget for a call that blocks server-side for ``timeout_ms``."""
    if timeout_ms is None:
        return TIMEOUT
    return timeout_ms / 1000 + TIMEOUT_MARGIN


def _read_message(connection: socket.socket) -> bytes:
    """Read one newline-terminated message off ``connection``.

    Responses routinely exceed a single ``recv`` (``agent.read`` returns
    whole scrollbacks), so this accumulates until the framing newline
    arrives or the peer hangs up.
    """
    chunks: list[bytes] = []
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


class HerdrClient:
    """Client for one herdr server, addressed by its unix socket.

    Every call opens its own connection, as herdr's own CLI does: connections
    are cheap, and a per-call socket keeps concurrent callers — the dispatch
    path and the event consumer — from interleaving on one stream. Long-lived
    subscriptions are the exception and get their own connection.
    """

    def __init__(self, session: str | None = None, path: str | None = None) -> None:
        self.session = session
        self.path = path or socket_path(session)

    def describe(self) -> str:
        """One line naming the session and socket this client resolved to.

        Kept here because this is where both facts are known: a caller that
        passed no session cannot otherwise say which one it ended up on.
        """
        return describe_session(self.session, self.path)

    def connect(self, timeout: float = TIMEOUT) -> socket.socket:
        """Open a connection to the server, for callers that hold it open."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            connection.connect(self.path)
        except OSError as exc:
            connection.close()
            raise HerdrTransportError(f"herdr socket {self.path}: {exc}") from exc
        return connection

    def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = TIMEOUT,
    ) -> dict:
        """Send ``method`` with ``params`` and return its ``result`` payload.

        ``params`` is always sent, even when empty: herdr rejects a request
        without the key outright (``invalid_request``), rather than defaulting
        it.
        """
        request = json.dumps({"id": "1", "method": method, "params": params or {}})
        try:
            with self.connect(timeout) as connection:
                connection.sendall(request.encode() + b"\n")
                payload = _read_message(connection)
        except OSError as exc:
            raise HerdrTransportError(f"herdr {method}: {exc}") from exc
        return _unwrap(method, payload)

    def subscribe(
        self,
        subscriptions: list[dict],
        tick: float | None = None,
    ) -> Iterator[dict | None]:
        """Yield event envelopes from a long-lived subscription.

        Unlike :meth:`call`, this holds its connection open and blocks
        between events — an idle fleet is silent for hours — so it gets its
        own socket. With ``tick`` set, a quiet period that long yields
        ``None`` instead of blocking on, which gives the caller a chance to
        act on time rather than only on traffic. The server's subscription
        acknowledgement is swallowed; only ``{event, data}`` envelopes are
        yielded. Returns when the server hangs up.
        """
        with self.connect() as connection:
            connection.settimeout(tick)
            request = json.dumps(
                {
                    "id": "subscribe",
                    "method": "events.subscribe",
                    "params": {"subscriptions": subscriptions},
                }
            )
            try:
                connection.sendall(request.encode() + b"\n")
                buffer = b""
                while True:
                    try:
                        chunk = connection.recv(65536)
                    except TimeoutError:
                        yield None
                        continue
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        message = json.loads(line)
                        error = message.get("error")
                        if error is not None:
                            raise HerdrAPIError(
                                error.get("code", "unknown"),
                                error.get("message", ""),
                            )
                        if "event" in message:
                            yield message
            except OSError as exc:
                raise HerdrTransportError(f"herdr subscription: {exc}") from exc
            except ValueError as exc:
                raise HerdrTransportError(
                    f"herdr subscription sent invalid JSON: {exc}"
                ) from exc

    def ping(self) -> dict:
        """Return herdr's ``{version, protocol, capabilities}`` banner."""
        return self.call("ping")

    def check_protocol(self, expected: int = PROTOCOL) -> None:
        """Raise unless the server speaks protocol ``expected``.

        Called at bridge startup: a protocol drift means every later call is
        suspect, so it is better to refuse to start (docs/PLAN.md §5.8).
        """
        banner = self.ping()
        actual = banner.get("protocol")
        if actual != expected:
            raise HerdrError(
                f"herdr at {self.path} speaks protocol {actual}, "
                f"but this client targets {expected}"
            )


def _unwrap(method: str, payload: bytes) -> dict:
    """Decode a response body into its ``result``, raising on any error."""
    try:
        response = json.loads(payload)
    except ValueError as exc:
        raise HerdrTransportError(
            f"herdr {method}: response was not JSON: {payload!r}"
        ) from exc
    error = response.get("error")
    if error is not None:
        raise HerdrAPIError(
            error.get("code", "unknown"), error.get("message", "")
        )
    result = response.get("result")
    if result is None:
        raise HerdrTransportError(f"herdr {method}: response carried no result")
    return result
