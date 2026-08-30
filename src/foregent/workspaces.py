"""Per-issue jj workspaces, created at dispatch and removed at completion (JIM-59).

Foregent owns the workspace lifecycle. The bridge builds an agent's checkout
before launching it and removes it when the issue completes, so no agent
inherits the previous one's dirty working copy and a crashed agent leaks
nothing a later dispatch cannot reclaim.

The unit is a **jj workspace**, named for the issue key. Creation is
``jj workspace forget`` then ``jj workspace add``: forgetting first is what
makes the name reclaimable, so there is no reaper and no registry to keep
honest.

Two properties of a secondary jj workspace shape everything here, both
established by driving jj 0.43 directly:

- It has no ``.git``. Raw ``git`` and ``gh`` are blind inside one, which the
  write paths survive because ``jj git push`` reaches the shared git backend
  and Pull Request mode opens its pull request over the API.
- A bookmark it moves is **not exported to git** until a mutating jj command
  runs at the colocated root, and a workspace's own working copy is reachable
  from the root as the revset ``<name>@``. Both are why bootstrap mode lands
  its work through :func:`advance`, run at the repo root, rather than from
  inside the workspace where git would not see it.

A fresh workspace holds only what version control tracks, so the untracked
files a project needs to run — ``.env`` and its kind — are carried over from
a ``.worktreeinclude`` manifest at the repo root (JIM-147). See
:func:`copy_included`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from foregent import config, mcp_servers
from foregent.models import Mode

logger = logging.getLogger(__name__)

# The revision a fresh workspace starts on. Hardcoded rather than configured:
# bootstrap mode already names `main` as the branch the agent rebases onto and
# fast-forwards (docs/ARCHITECTURE.md §6.4), and the worker skill tells agents
# the same, so a second name for it would be a third place to disagree.
TRUNK = "main"

# The manifest of untracked files to carry into every workspace, read at the
# repo root. The name and the format are Claude Code's convention rather than
# foregent's, so a project already carrying files into `claude --worktree`
# checkouts gets the same set here with nothing to configure twice.
INCLUDE_FILE = ".worktreeinclude"

# The remote whose URL decides a project's mode, and what makes that URL a
# GitHub one. `origin` is git's own name for the remote a repo was cloned
# from, so a project with a pull request to open has one; a repo with no
# remote at all has nowhere to open one.
ORIGIN = "origin"
GITHUB = "github.com"

# Per-call budget for a jj subprocess. Creating a workspace writes a whole
# working copy, so this is generous; it exists to stop a wedged jj from
# hanging dispatch forever, not to bound normal work.
TIMEOUT = 300


class WorkspaceError(Exception):
    """Raised when a workspace could not be created or removed."""


def is_repo(directory: Path) -> bool:
    """Whether ``directory`` is the root of a jj repo.

    Keyed on ``.jj`` rather than on ``jj`` exiting cleanly, so the check costs
    no subprocess and gives the same answer on a box with no jj installed.
    """
    return (directory / ".jj").is_dir()


def mode_for(repo: Path) -> Mode:
    """How ``repo`` wants its work landed, read off its git remotes.

    Pull Request mode needs somewhere to open a pull request, so the question
    is whether one exists: an ``origin`` remote on GitHub means yes, and
    anything else — no remotes, an ``origin`` hosted elsewhere, a directory
    that is not a jj repo at all — means bootstrap, which needs nothing.
    ``github.com`` is matched anywhere in the URL, which covers the HTTPS and
    the SSH spelling alike.

    Derived rather than declared: a project that says one thing in a file and
    another in its remotes can only disagree with itself. Both halves of the
    contract come from here — the mode the agent is briefed with at dispatch,
    and the one the bridge completes the issue in.

    A jj command that fails answers bootstrap rather than raising. Refusing to
    dispatch over an unreadable remote list would be a worse answer than
    dispatching an agent that commits locally.
    """
    if not is_repo(repo):
        return Mode.BOOTSTRAP
    try:
        listed = _jj(repo, "git", "remote", "list")
    except WorkspaceError as exc:
        logger.warning("could not read %s's git remotes: %s", repo, exc)
        return Mode.BOOTSTRAP
    for line in listed.splitlines():
        name, _, url = line.partition(" ")
        if name == ORIGIN and GITHUB in url:
            return Mode.PULL_REQUEST
    return Mode.BOOTSTRAP


def path_for(key: str) -> Path:
    """Where the workspace for issue ``key`` lives."""
    return config.workspace_root() / key


def repo_for(path: Path) -> Path | None:
    """The repo the workspace at ``path`` belongs to, or ``None`` if it is not one.

    A secondary workspace's ``.jj/repo`` is a *file* naming the shared repo
    directory, written relative to the ``.jj`` that holds it; the repo root is
    that directory's grandparent. Reading it is what lets foregent tear a
    workspace down after a restart, where the agent's cwd is recoverable from
    the harness but the repo it was built from is remembered nowhere
    (:func:`foregent.server.rebuild_store`).

    Anything else answers ``None`` and is left alone: a repo's own root, whose
    ``.jj/repo`` is a directory rather than a file, and a plain directory
    foregent ran an agent in because the project is not a jj repo at all.
    """
    jj = path / ".jj"
    try:
        named = (jj / "repo").read_text().strip()
    except OSError:
        return None
    # `named` is absolute in some jj versions and relative to `.jj` in others;
    # joining handles both, since a `/` with an absolute right-hand side is
    # that path.
    return (jj / named).resolve().parent.parent


def create(repo: Path, key: str) -> Path:
    """Build a fresh workspace for ``key`` under ``repo`` and return its path.

    A ``repo`` that is not a jj repo is returned unchanged and used as the
    agent's cwd directly, which is what foregent did before per-issue
    workspaces existed. That keeps a non-jj project working rather than
    failing its dispatch over a feature it cannot use.

    The workspace is forgotten before it is added, so a stale one left by a
    crashed agent is reclaimed by the next dispatch that wants the name.
    """
    if not is_repo(repo):
        logger.info("%s is not a jj repo; running the agent in it directly", repo)
        return repo
    path = path_for(key)
    # Forget before add, and clear the directory with it: `jj workspace add`
    # refuses a destination that already exists, so a crashed agent's leftovers
    # would otherwise block every future dispatch for that key.
    _forget(repo, key)
    _remove(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # `-r` is not optional. With no revision, jj gives the new working copy the
    # parents of the *current* workspace's working-copy commit — whichever
    # commit the operator's own checkout happens to be sitting on — so the
    # agent would start from wherever they last were.
    _jj(repo, "workspace", "add", "--name", key, "-r", TRUNK, str(path))
    copy_included(repo, path)
    ensure_trusted(path)
    logger.info("created the %s workspace at %s", key, path)
    return path


def advance(repo: Path, key: str) -> None:
    """Move ``TRUNK`` onto the work the ``key`` workspace holds.

    This is how bootstrap mode lands a change: the agent commits on top of
    ``main`` and stops there, and the bridge moves the bookmark when the issue
    completes. Keeping it here rather than in the agent means one worker that
    forgets, or moves it somewhere else, cannot decide what ``main`` is.

    Two details are load-bearing, both established by driving jj 0.43:

    - **The revision is** ``<key>@-``, the parent of the workspace's
      working-copy commit. ``<key>@`` is a revset for another workspace's
      working copy, so the repo root can name the agent's tip without entering
      the workspace; its parent, rather than itself, because the working-copy
      commit is jj's scratch space and publishing it would put an empty commit
      at the head of ``main``. An agent that committed nothing leaves ``@-``
      *on* ``main``, which jj answers with "No bookmarks to update" and a zero
      exit.
    - **``bookmark move`` is fast-forward-only** without ``--allow-backwards``,
      so jj refuses work that is not descended from ``main`` and leaves the
      bookmark where it was. The rebase requirement the worker skill states is
      enforced here for free, with no ancestry revset of foregent's own to get
      wrong.

    Running at the colocated repo root is also what exports the bookmark to
    git: a mutating jj command there is what git's view of ``main`` waits for.
    """
    if not is_repo(repo):
        return
    _jj(repo, "bookmark", "move", TRUNK, "--to", f"{key}@-")
    logger.info("advanced %s onto the work in the %s workspace", TRUNK, key)


def destroy(repo: Path, key: str, path: Path) -> None:
    """Remove the workspace for ``key``.

    ``forget`` runs even when ``path`` is already gone, so a workspace whose
    directory a crash took with it stops being one jj still knows about.
    Nothing is published here: :func:`advance` has already moved the bookmark
    at the colocated root, so this is cleanup and a failure costs a directory
    rather than the work in it.
    """
    if not is_repo(repo):
        return
    _forget(repo, key)
    _remove(path)
    logger.info("removed the %s workspace at %s", key, path)


def _forget(repo: Path, key: str) -> None:
    """Forget the workspace named ``key``, tolerating one that is not there.

    jj answers an unknown workspace with a warning and a zero exit, so absence
    needs no probe of its own.
    """
    _jj(repo, "workspace", "forget", key)


def _remove(path: Path) -> None:
    """Delete ``path`` and everything under it, tolerating absence."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkspaceError(f"could not remove {path}: {exc}") from exc


def _jj(repo: Path, *args: str) -> str:
    """Run one jj command in ``repo`` and return its stdout."""
    try:
        done = subprocess.run(
            ["jj", "--no-pager", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=True,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError("jj is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"jj {' '.join(args)} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise WorkspaceError(f"jj {' '.join(args)} failed: {detail}") from exc
    return done.stdout


def copy_included(repo: Path, path: Path) -> list[str]:
    """Carry ``repo``'s ``.worktreeinclude`` files into the workspace at ``path``.

    A fresh workspace checks out only what version control tracks, so the
    untracked files a project needs to run — ``.env``, a local settings file, a
    key — are not in it. ``.worktreeinclude`` is `Claude Code's convention
    <https://code.claude.com/docs/en/worktrees#copy-gitignored-files-into-worktrees>`_
    for naming them: a manifest at the repo root in ``.gitignore`` syntax,
    whose entries are copied into every new checkout.

    A file is carried over when it matches the manifest **and** is itself
    ignored, which is the convention's own rule and keeps a tracked file from
    being duplicated as an untracked copy of itself. Both halves are answered
    by :func:`_listed` rather than by a pattern matcher here, so the syntax is
    git's own down to the corners — a bare directory name expands to the files
    under it, and a file inside a wholly ignored directory is still found.

    Returns the paths copied, relative to the repo. Absence of the manifest,
    of git, or of the ``.git`` a colocated repo has is not an error: a project
    that carries nothing over still dispatches.
    """
    if not (repo / INCLUDE_FILE).is_file():
        return []
    try:
        wanted = _listed(repo, "--exclude-from", INCLUDE_FILE)
        ignored = _listed(repo, "--exclude-standard")
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not read %s in %s: %s", INCLUDE_FILE, repo, exc)
        return []
    names = sorted(wanted & ignored)
    for name in names:
        _copy(repo / name, path / name)
    if names:
        logger.info("copied %d %s file(s) into %s", len(names), INCLUDE_FILE, path)
    return names


def _listed(repo: Path, *exclude: str) -> set[str]:
    """The untracked files in ``repo`` that ``exclude``'s patterns match.

    ``--others --ignored`` is what pairs with an exclude option to mean "the
    files these patterns pick out", so the same call answers both halves of
    :func:`copy_included`'s rule by being handed a different source of
    patterns. A symlink is one entry and is never followed, a directory
    symlink included, which is what lets the copy re-point it rather than
    walking through it.

    Raises ``OSError`` when git cannot answer — it is missing, or the repo is
    a jj repo that is not colocated and so has no ``.git`` — and
    ``TimeoutExpired`` when it wedges. Both leave the manifest uncopied rather
    than the dispatch failed.
    """
    done = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--ignored", *exclude],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout or "").strip())
    return {name for name in done.stdout.split("\0") if name}


def _copy(source: Path, destination: Path) -> None:
    """Reproduce ``source`` at ``destination``, preserving a symlink as one.

    A symlink is re-pointed at the file the original names, not merely given
    the same link text: a workspace lives under ``FOREGENT_WORKSPACE_ROOT``,
    nowhere near the repo, so a relative target copied verbatim would resolve
    against the wrong directory and dangle. It is made absolute against the
    source's own directory; an already-absolute target is written through
    unchanged.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target = Path(os.readlink(source))
            if not target.is_absolute():
                target = source.parent / target
            destination.symlink_to(target)
        else:
            shutil.copy2(source, destination)
    except OSError as exc:
        raise WorkspaceError(f"could not copy {source} to {destination}: {exc}") from exc


def ensure_trusted(path: Path) -> bool:
    """Record ``path`` as a trusted workspace if it is not one already.

    Claude Code opens its ``Yes, I trust this folder`` dialog in a directory it
    has not seen, and herdr reads that dialog as ``blocked``, so an untrusted
    cwd does not slow a dispatch down — it fails it, with nobody there to
    answer. A per-issue workspace is always a fresh directory, and its path is
    not known before the issue is queued, so the operator cannot pre-accept it
    by hand the way `README.md` describes for a fixed one.

    Returns whether an entry was written. **Trust is checked before it is
    written, and a trusted path is left alone**, which is what keeps this
    honest about :func:`foregent.mcp_servers.config_file`'s rule that every
    running Claude Code session rewrites that file so foregent must not. On a
    box whose workspace root is trusted, :func:`trusted` answers yes by
    inheritance and nothing here writes at all; the write is the fallback for
    a box where it is not, where the alternative is a dispatch that hangs on a
    dialog rather than one that runs.
    """
    if trusted(path):
        return False
    _write_trust(path)
    logger.info("recorded %s as a trusted workspace for Claude Code", path)
    return True


def trusted(path: Path) -> bool:
    """Whether Claude Code would open its trust dialog in ``path``.

    Mirrors the harness's own rule: an exact entry for the directory, or one
    on any ancestor of it. The ancestor walk is why trusting a workspace root
    once covers every per-issue workspace under it.

    Read off Claude Code 2.1.251 and not documented by it, so it is treated as
    an optimization rather than a guarantee — :func:`ensure_trusted` writes the
    exact entry whenever this says no, which is the answer a stricter harness
    would give.
    """
    projects = _config().get("projects")
    if not isinstance(projects, dict):
        return False
    for directory in (path, *path.parents):
        entry = projects.get(str(directory))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _config() -> dict:
    """Claude Code's user-level config, or an empty one if it cannot be read.

    An unreadable or malformed config reads as "nothing is trusted", so the
    caller writes an entry rather than assuming a dispatch will work.
    """
    try:
        loaded = json.loads(mcp_servers.config_file().read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_trust(path: Path) -> None:
    """Add ``path`` to the config's trusted projects, atomically.

    Staged and renamed so a session reading the file never sees a partial one.
    The read-modify-write can still lose whatever a session wrote in between;
    that is why the caller only reaches this when the path is genuinely
    untrusted, which on a correctly provisioned box is never.
    """
    config_file = mcp_servers.config_file()
    loaded = _config()
    projects = loaded.get("projects")
    loaded["projects"] = projects if isinstance(projects, dict) else {}
    entry = loaded["projects"].get(str(path))
    loaded["projects"][str(path)] = {
        **(entry if isinstance(entry, dict) else {}),
        "hasTrustDialogAccepted": True,
    }
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=config_file.parent,
            prefix=f".{config_file.name}.",
            delete=False,
        ) as staged:
            json.dump(loaded, staged, indent=2)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged.name, config_file)
    except OSError as exc:
        raise WorkspaceError(f"could not record {path} as trusted: {exc}") from exc
