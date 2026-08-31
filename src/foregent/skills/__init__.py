"""The skills foregent ships, and where they are installed (JIM-91).

Foregent's skills are Claude Code *personal* skills: they live in the box's
user-level skill directory (``~/.claude/skills/<name>/SKILL.md``), the one
place a session loads for every project. The skill files themselves sit
alongside this module inside the installed package, so they travel with a
``uv tool install foregent`` rather than only existing in a repo checkout.

Two callers install them through the one function :func:`install`, so what
an agent is briefed from cannot drift from what foregent ships:

- ``foregent setup``, run once per machine and again after upgrading foregent.
- The server, before launching an agent. An agent is briefed from the skill on
  disk, so dispatch refreshes it: a copy left over from an older foregent
  would otherwise brief every agent silently, and nothing downstream can tell.
"""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
from pathlib import Path

PACKAGED = Path(__file__).resolve().parent
"""The directory holding the packaged skills — this module's own."""

DEFAULT_CONFIG_DIR = "~/.claude"


class Outcome(StrEnum):
    """What installing one skill did, for the operator-facing report."""

    INSTALLED = "installed"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def skills_root() -> Path:
    """The user-level Claude Code skill directory to install into.

    ``CLAUDE_CONFIG_DIR`` relocates the whole of ``~/.claude``, so honor it
    rather than hardcoding the home-relative path — a box that sets it has no
    ``~/.claude`` for Claude Code to read at all.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR") or DEFAULT_CONFIG_DIR
    return Path(config).expanduser() / "skills"


def packaged() -> list[Path]:
    """Every skill directory foregent ships, sorted by name.

    Keyed on ``SKILL.md`` so this module's own ``__init__.py`` and any
    ``__pycache__`` beside it are never mistaken for a skill.
    """
    return sorted(path.parent for path in PACKAGED.glob("*/SKILL.md"))


def install(root: Path | None = None) -> list[tuple[str, Outcome]]:
    """Install every packaged skill under ``root``, overwriting stale copies.

    Returns ``(name, outcome)`` per skill so the caller can say what it did:
    a re-run after upgrading foregent should be legible, and an operator whose
    edits were replaced should be told rather than left to discover it.
    """
    root = root if root is not None else skills_root()
    return [(skill.name, _install(skill, root)) for skill in packaged()]


def _install(skill: Path, root: Path) -> Outcome:
    """Copy one packaged skill directory into ``root``."""
    target = root / skill.name
    files = [(source, target / source.relative_to(skill)) for source in _files(skill)]
    # Presence is keyed on SKILL.md, not on the directory: that file is what
    # the loader reads, and an empty directory left by a half-finished copy
    # must not read as installed.
    if (target / "SKILL.md").is_file():
        if all(
            destination.is_file() and destination.read_bytes() == source.read_bytes()
            for source, destination in files
        ):
            return Outcome.UNCHANGED
        outcome = Outcome.UPDATED
    else:
        outcome = Outcome.INSTALLED
    for source, destination in files:
        _write_atomically(source, destination)
    return outcome


def _files(skill: Path) -> list[Path]:
    """Every file in a skill directory, including any supporting subdirectory."""
    return sorted(path for path in skill.rglob("*") if path.is_file())


def _write_atomically(source: Path, destination: Path) -> None:
    """Copy ``source`` over ``destination`` without ever exposing a partial file.

    Two dispatches can reach the ensure step at once, and a Claude Code
    session watching the directory picks up whatever is on disk the moment it
    appears. Staging in the destination's own directory keeps the rename on
    one filesystem, which is what makes it atomic.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(source.read_bytes())
        os.replace(staged, destination)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
