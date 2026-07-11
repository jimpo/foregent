"""The ``foregent`` management CLI.

A thin client over the foregent API server (:mod:`foregent.server`). The
``status`` subcommand fetches tracked issues over HTTP and pretty-prints them;
``serve`` runs the server itself. The store lives in the server, not here.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from urllib.parse import urlparse

from foregent import __version__
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

    serve = subparsers.add_parser(
        "serve",
        help="Run the foregent API server.",
        description="Serve the issue store over HTTP for the CLI to query.",
    )
    serve.set_defaults(func=cmd_serve)

    return parser


def fetch_issues() -> list[Issue]:
    """Fetch the tracked issues from the server, deserialized into `Issue`."""
    with urllib.request.urlopen(f"{api_url()}/issues") as response:
        records = json.load(response)
    return [
        Issue(key=r["key"], title=r["title"], status=IssueStatus(r["status"]))
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
        print(
            f"{issue.key:<{key_width}}  "
            f"{issue.status:<{status_width}}  "
            f"{issue.title}"
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API server on the host/port from the configured API URL."""
    import uvicorn

    url = urlparse(api_url())
    uvicorn.run("foregent.server:app", host=url.hostname or "127.0.0.1", port=url.port or 8577)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
