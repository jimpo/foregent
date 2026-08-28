"""Checks on per-issue jj workspaces (JIM-59).

These drive a real ``jj`` against throwaway repos rather than mocking the
subprocess. The whole feature is a claim about what jj does — that forgetting
a workspace reclaims its name, that a fresh one starts on trunk, that
forgetting one exports the bookmark it moved — and a mocked ``subprocess.run``
would assert only that foregent still passes the arguments it used to.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foregent import workspaces

JJ = shutil.which("jj")


def jj(repo: Path, *args: str) -> str:
    """Run a jj command in ``repo``, for arranging and inspecting fixtures."""
    done = subprocess.run(
        ["jj", "--no-pager", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@unittest.skipUnless(JJ, "jj is not installed")
class WorkspaceTest(unittest.TestCase):
    """Creating and removing a workspace against a real colocated repo."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        # Colocated, because that is what foregent manages and what makes the
        # git-export behavior below observable at all.
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        jj(self.repo, "git", "init", "--colocate")
        (self.repo / "a.txt").write_text("a\n")
        jj(self.repo, "describe", "-m", "first")
        jj(self.repo, "new")
        jj(self.repo, "bookmark", "create", "main", "-r", "@-")
        jj(self.repo, "git", "export")
        self.root = self.tmp / "workspaces"
        # Trust is a separate concern with its own tests; keep it out of the
        # way here so these never touch the real ~/.claude.json.
        patches = [
            mock.patch.object(workspaces.config, "workspace_root", lambda: self.root),
            mock.patch.object(workspaces, "ensure_trusted", lambda path: False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_create_places_the_workspace_on_trunk(self) -> None:
        """A fresh workspace starts on ``main``, not on the operator's checkout."""
        # Move the default workspace off main, which is what jj would otherwise
        # hand the new workspace as its parent.
        (self.repo / "scratch.txt").write_text("scratch\n")
        jj(self.repo, "describe", "-m", "operator's unrelated work")

        path = workspaces.create(self.repo, "JIM-1")

        self.assertEqual(path, self.root / "JIM-1")
        self.assertTrue((path / "a.txt").is_file())
        self.assertFalse((path / "scratch.txt").exists())
        parent = jj(path, "log", "--no-graph", "-r", "@-", "-T", "description")
        self.assertEqual(parent.strip(), "first")

    def test_create_reclaims_a_stale_workspace(self) -> None:
        """A crashed agent's leftovers do not block the next dispatch for its key."""
        first = workspaces.create(self.repo, "JIM-1")
        (first / "leftover.txt").write_text("from the agent that died\n")

        second = workspaces.create(self.repo, "JIM-1")

        self.assertEqual(first, second)
        self.assertFalse((second / "leftover.txt").exists())
        self.assertEqual(_names(self.repo).count("JIM-1"), 1)

    def test_destroy_removes_the_workspace(self) -> None:
        path = workspaces.create(self.repo, "JIM-1")

        workspaces.destroy(self.repo, "JIM-1", path)

        self.assertFalse(path.exists())
        self.assertNotIn("JIM-1", _names(self.repo))

    def test_destroy_exports_the_bookmark_the_agent_moved(self) -> None:
        """Teardown is what publishes bootstrap-mode work to git.

        A bookmark moved inside a secondary workspace is invisible to git until
        a mutating jj command runs at the colocated root. If ``destroy`` ever
        stops running ``forget`` there, an agent's fast-forward of ``main``
        silently stops reaching git, and nothing else in the system notices.
        """
        path = workspaces.create(self.repo, "JIM-1")
        (path / "b.txt").write_text("the agent's work\n")
        jj(path, "describe", "-m", "the agent's work")
        jj(path, "new")
        jj(path, "bookmark", "set", "main", "-r", "@-")
        self.assertEqual(_git_head(self.repo), "first")

        workspaces.destroy(self.repo, "JIM-1", path)

        self.assertEqual(_git_head(self.repo), "the agent's work")

    def test_destroy_forgets_even_when_the_directory_is_gone(self) -> None:
        """The export must not be skipped because the checkout vanished."""
        path = workspaces.create(self.repo, "JIM-1")
        jj(path, "bookmark", "set", "main", "-r", "@")
        (path / "b.txt").write_text("work\n")
        jj(path, "describe", "-m", "work in a doomed directory")
        jj(path, "bookmark", "set", "main", "-r", "@")
        shutil.rmtree(path)

        workspaces.destroy(self.repo, "JIM-1", path)

        self.assertNotIn("JIM-1", _names(self.repo))
        self.assertEqual(_git_head(self.repo), "work in a doomed directory")

    def test_destroy_is_repeatable(self) -> None:
        """Completing twice must not fail; retrying a completion is safe."""
        path = workspaces.create(self.repo, "JIM-1")
        workspaces.destroy(self.repo, "JIM-1", path)
        workspaces.destroy(self.repo, "JIM-1", path)

    def test_two_workspaces_coexist(self) -> None:
        """Both hold `main` at once, which git worktrees cannot do."""
        first = workspaces.create(self.repo, "JIM-1")
        second = workspaces.create(self.repo, "JIM-2")

        self.assertNotEqual(first, second)
        self.assertTrue((first / "a.txt").is_file())
        self.assertTrue((second / "a.txt").is_file())

    def test_a_non_jj_directory_is_used_directly(self) -> None:
        """A project foregent cannot isolate still gets its agent."""
        plain = self.tmp / "plain"
        plain.mkdir()

        self.assertEqual(workspaces.create(plain, "JIM-1"), plain)
        self.assertFalse((self.root / "JIM-1").exists())
        # And teardown leaves the operator's own directory alone.
        workspaces.destroy(plain, "JIM-1", plain)
        self.assertTrue(plain.is_dir())

    def test_a_failing_jj_command_raises(self) -> None:
        """jj's own message reaches the operator rather than a bare exit code.

        A repo with no trunk is the realistic way this fails: dispatch into a
        project whose main branch is named something else.
        """
        with mock.patch.object(workspaces, "TRUNK", "no-such-bookmark"):
            with self.assertRaises(workspaces.WorkspaceError) as caught:
                workspaces.create(self.repo, "JIM-1")

        self.assertIn("no-such-bookmark", str(caught.exception))


def _names(repo: Path) -> list[str]:
    """The workspace names jj knows about in ``repo``."""
    listing = jj(repo, "workspace", "list")
    return [line.split(":", 1)[0] for line in listing.splitlines() if ":" in line]


def _git_head(repo: Path) -> str:
    """The subject of the commit git's ``main`` points at."""
    done = subprocess.run(
        ["git", "log", "-1", "--format=%s", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class TrustTest(unittest.TestCase):
    """Recording a workspace as trusted, and knowing when not to."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / ".claude.json"
        patch = mock.patch.object(
            workspaces.mcp_servers, "config_file", lambda: self.config
        )
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, payload: dict) -> None:
        self.config.write_text(json.dumps(payload))

    def projects(self) -> dict:
        return json.loads(self.config.read_text())["projects"]

    def test_an_unknown_directory_is_recorded(self) -> None:
        self.write({"projects": {}})

        self.assertTrue(workspaces.ensure_trusted(Path("/ws/JIM-1")))

        self.assertIs(
            self.projects()["/ws/JIM-1"]["hasTrustDialogAccepted"],
            True,
        )

    def test_a_trusted_directory_is_left_alone(self) -> None:
        """The file every Claude Code session rewrites is not written needlessly."""
        self.write({"projects": {"/ws/JIM-1": {"hasTrustDialogAccepted": True}}})
        before = self.config.read_text()

        self.assertFalse(workspaces.ensure_trusted(Path("/ws/JIM-1")))

        self.assertEqual(self.config.read_text(), before)

    def test_trust_is_inherited_from_an_ancestor(self) -> None:
        """Trusting the workspace root once covers every workspace under it."""
        self.write({"projects": {"/ws": {"hasTrustDialogAccepted": True}}})
        before = self.config.read_text()

        self.assertTrue(workspaces.trusted(Path("/ws/JIM-1")))
        self.assertFalse(workspaces.ensure_trusted(Path("/ws/JIM-1")))

        self.assertEqual(self.config.read_text(), before)

    def test_an_untrusted_ancestor_does_not_count(self) -> None:
        self.write({"projects": {"/ws": {"hasTrustDialogAccepted": False}}})

        self.assertFalse(workspaces.trusted(Path("/ws/JIM-1")))

    def test_writing_preserves_what_the_config_already_held(self) -> None:
        """A read-modify-write must not cost the operator their other settings."""
        self.write(
            {
                "projects": {
                    "/other": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]}
                },
                "mcpServers": {"linear": {"type": "http"}},
            }
        )

        workspaces.ensure_trusted(Path("/ws/JIM-1"))

        loaded = json.loads(self.config.read_text())
        self.assertEqual(loaded["mcpServers"], {"linear": {"type": "http"}})
        self.assertEqual(loaded["projects"]["/other"]["allowedTools"], ["Bash"])

    def test_an_existing_entry_keeps_its_other_keys(self) -> None:
        """A directory Claude Code already knows keeps its history."""
        self.write({"projects": {"/ws/JIM-1": {"allowedTools": ["Read"]}}})

        workspaces.ensure_trusted(Path("/ws/JIM-1"))

        entry = self.projects()["/ws/JIM-1"]
        self.assertEqual(entry["allowedTools"], ["Read"])
        self.assertIs(entry["hasTrustDialogAccepted"], True)

    def test_an_absent_config_is_created(self) -> None:
        """A box where Claude Code has never run still dispatches."""
        self.assertTrue(workspaces.ensure_trusted(Path("/ws/JIM-1")))

        self.assertIs(self.projects()["/ws/JIM-1"]["hasTrustDialogAccepted"], True)

    def test_a_malformed_config_reads_as_untrusted(self) -> None:
        """Better to write an entry than to assume a dispatch will work."""
        self.config.write_text("{ not json")

        self.assertFalse(workspaces.trusted(Path("/ws/JIM-1")))

    def test_the_config_is_replaced_atomically(self) -> None:
        """A session reading mid-write must never see a partial config."""
        self.write({"projects": {}})
        with mock.patch.object(workspaces.os, "replace") as replace:
            workspaces.ensure_trusted(Path("/ws/JIM-1"))

        replace.assert_called_once()
        staged, destination = replace.call_args.args
        self.assertEqual(Path(destination), self.config)
        self.assertEqual(Path(staged).parent, self.config.parent)
        os.unlink(staged)


if __name__ == "__main__":
    unittest.main()
