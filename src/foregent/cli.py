"""The ``foregent`` management CLI.

A thin client over the foregent API server (:mod:`foregent.server`). The
``status`` subcommand fetches tracked issues over HTTP and pretty-prints them;
``serve`` runs the server itself. The store lives in the server, not here.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from urllib.parse import quote, urlparse

from foregent import __version__, mcp_servers, skills
from foregent.config import api_url
from foregent.models import Issue, IssueStatus


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="foregent",
        description="Manage and observe foregent's autonomous issue work.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser(
        "status",
        help="Show the status of tracked issues.",
        description="List every issue foregent is tracking and its status.",
    )
    status.set_defaults(func=cmd_status)

    queue = subparsers.add_parser(
        "queue",
        help="Queue an issue for an autonomous agent.",
        description=(
            "Ask the server to queue an issue; an agent is launched for it "
            "as soon as capacity allows."
        ),
    )
    queue.add_argument("issue_id", help="Linear issue key, e.g. JIM-49.")
    queue.add_argument(
        "-d",
        "--directory",
        default=".",
        help="Working directory for the agent (default: current directory).",
    )
    queue.set_defaults(func=cmd_queue)

    setup = subparsers.add_parser(
        "setup",
        help="Prepare this machine's Claude Code configuration for foregent.",
        description=(
            "Copy every skill foregent ships into the user-level Claude Code "
            "skill directory, and add the Linear and GitHub MCP servers to "
            "the machine's user config, where agents and your own sessions "
            "both load them from. Run once per machine, and again after "
            "upgrading foregent."
        ),
    )
    setup.set_defaults(func=cmd_setup)

    serve = subparsers.add_parser(
        "serve",
        help="Run the foregent API server.",
        description="Serve the issue store over HTTP for the CLI to query.",
    )
    serve.add_argument(
        "--dev",
        action="store_true",
        help="Enable autoreload: restart the server when source files change.",
    )
    serve.set_defaults(func=cmd_serve)

    return parser


def fetch_issues() -> list[Issue]:
    """Fetch the tracked issues from the server, deserialized into `Issue`."""
    with urllib.request.urlopen(f"{api_url()}/issues") as response:
        records = json.load(response)
    return [
        Issue(
            key=r["key"],
            title=r["title"],
            status=IssueStatus(r["status"]),
            blocker=r.get("blocker", ""),
        )
        for r in records
    ]


def cmd_status(args: argparse.Namespace) -> int:
    """Fetch tracked issues from the server and print them as a table."""
    try:
        issues = fetch_issues()
    except urllib.error.URLError as exc:
        print(
            f"Cannot reach foregent server at {api_url()}: {exc.reason}",
            file=sys.stderr,
        )
        return 1

    if not issues:
        print("No issues are being tracked.")
        return 0

    key_width = max(len("ISSUE"), *(len(issue.key) for issue in issues))
    status_width = max(len("STATUS"), *(len(issue.status) for issue in issues))
    print(f"{'ISSUE':<{key_width}}  {'STATUS':<{status_width}}  TITLE")
    for issue in issues:
        title = issue.title
        if issue.blocker:
            title = f"{title}  (blocked on {issue.blocker})"
        print(
            f"{issue.key:<{key_width}}  "
            f"{issue.status:<{status_width}}  "
            f"{title}"
        )
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Queue an issue on the server and print its resulting status."""
    request = urllib.request.Request(
        f"{api_url()}/issues/{quote(args.issue_id, safe='')}/queue",
        data=json.dumps({"directory": os.path.abspath(args.directory)}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            record = json.load(response)
    except urllib.error.HTTPError as exc:
        # The body is FastAPI's {"detail": str} on our server, but don't
        # assume: fall back to the HTTP reason on anything else. A 502 means
        # the issue queued but dispatch failed, so don't claim queueing failed.
        try:
            detail = json.load(exc)["detail"]
        except (ValueError, KeyError):
            detail = exc.reason
        if not isinstance(detail, str):
            detail = exc.reason
        print(f"{args.issue_id}: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"Cannot reach foregent server at {api_url()}: {exc.reason}",
            file=sys.stderr,
        )
        return 1
    print(f"{record['key']}: {record['status']}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Prepare this machine: install the packaged skills and the MCP servers."""
    root = skills.skills_root()
    print(f"Installing foregent's skills into {root}")
    for name, outcome in skills.install(root):
        print(f"  {outcome:<9}  {name}")

    print(f"Adding MCP servers to {mcp_servers.config_file()}")
    try:
        for name, installed in mcp_servers.install():
            print(f"  {'added' if installed else 'kept':<9}  {name}")
    except mcp_servers.MCPError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    # The config stores `${LINEAR_API_KEY}`, not the token: a server whose
    # variable is unset is configured but cannot authenticate, and an agent
    # only discovers that once it is already working an issue.
    absent = mcp_servers.missing_credentials()
    if absent:
        print(
            "  warning: MCP authentication will fail; unset in this "
            f"environment: {', '.join(absent)}",
            file=sys.stderr,
        )
    return 0


def serve_log_config() -> dict:
    """uvicorn's logging config, extended to carry foregent's own records.

    uvicorn configures only its own loggers, so without this everything the
    bridge logs below WARNING — which session it resolved, which skills it
    installed, which agent it adopted — reaches no handler and is dropped.
    Passed to `uvicorn.run` rather than set up here, because `--dev` runs the
    server in a reloader subprocess that configures logging from this config
    alone.
    """
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config["root"] = {"handlers": ["default"], "level": "INFO"}
    return config


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API server on the host/port from the configured API URL."""
    import uvicorn

    url = urlparse(api_url())
    uvicorn.run(
        "foregent.server:app",
        host=url.hostname or "127.0.0.1",
        port=url.port or 8577,
        reload=args.dev,
        log_config=serve_log_config(),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
