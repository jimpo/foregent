"""The ``foregent`` management CLI.

A thin reporting surface over an :class:`~foregent.store.IssueStore`. Today it
exposes a single ``status`` subcommand that lists tracked issues and their
lifecycle state; the store is empty in this skeleton, so ``status`` reports
that no issues are being tracked.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from foregent import __version__
from foregent.store import IssueStore


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

    return parser


def cmd_status(args: argparse.Namespace, store: IssueStore) -> int:
    """Print a table of tracked issues and their statuses."""
    issues = store.list_issues()
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # In-memory store: empty in the skeleton, rebuilt from CAO + Linear once
    # the bridge lands (docs/PLAN.md §5.11).
    store = IssueStore()

    func = args.func
    return func(args, store)


if __name__ == "__main__":
    raise SystemExit(main())
