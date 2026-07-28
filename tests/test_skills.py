"""Checks on the skills foregent ships and how they get installed (JIM-90, JIM-91).

A skill is prose, so there is little to assert about its content — but its
frontmatter is a contract with Claude Code's loader, and a skill that fails to
load fails silently: the agent just works the issue its own way and never
reports back. The same is true of a skill that never reaches the box at all,
so the packaging and the installer are guarded here too.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import foregent
from foregent import cli, skills

REPO = Path(__file__).resolve().parent.parent


def frontmatter(path: Path) -> dict[str, str]:
    """The YAML-ish header of a skill file, as simple key/value pairs.

    Deliberately not a YAML parse: the header is a handful of scalar keys, and
    the tests should not depend on a parser foregent does not otherwise need.
    """
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError(f"{path} does not open with a frontmatter fence")
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, separator, value = line.partition(":")
        if separator and not key.startswith(" "):
            fields[key.strip()] = value.strip().strip('"')
    raise AssertionError(f"{path} has an unterminated frontmatter block")


class SkillContentTests(unittest.TestCase):
    def test_every_skill_is_installable(self) -> None:
        # One SKILL.md per directory is the shape the installer copies and the
        # loader expects.
        self.assertTrue(skills.packaged(), "no skills found")
        for directory in sorted(p for p in skills.PACKAGED.iterdir() if p.is_dir()):
            if directory.name == "__pycache__":
                continue
            self.assertTrue(
                (directory / "SKILL.md").exists(),
                f"{directory} has no SKILL.md",
            )

    def test_frontmatter_names_match_their_directories(self) -> None:
        # The loader keys on the declared name; a mismatch makes a skill
        # unfindable under the name everything else refers to.
        for skill in skills.packaged():
            self.assertEqual(frontmatter(skill / "SKILL.md")["name"], skill.name)

    def test_every_skill_describes_when_to_use_it(self) -> None:
        # The description is what the model decides to load on.
        for skill in skills.packaged():
            fields = frontmatter(skill / "SKILL.md")
            self.assertGreater(len(fields.get("description", "")), 40)

    def test_the_worker_skill_covers_the_lifecycle_tools(self) -> None:
        # Foregent learns nothing about an issue's outcome except through
        # these, so a skill that omits them leaves the bridge blind.
        body = (skills.PACKAGED / "foregent-worker" / "SKILL.md").read_text()
        self.assertIn("complete_task", body)
        self.assertIn("report_blocked", body)


class PackagingTests(unittest.TestCase):
    """The skills have to exist wherever foregent is installed, not just here."""

    def test_the_skills_live_inside_the_installed_package(self) -> None:
        # A skills/ directory at the repo root does not travel with a
        # `uv tool install`, and both the setup command and the dispatch path
        # need a source that exists on the box foregent was installed onto.
        package = Path(foregent.__file__).resolve().parent
        for skill in skills.packaged():
            self.assertTrue(skill.is_relative_to(package))

    @unittest.skipUnless(shutil.which("uv"), "uv is not on PATH")
    @unittest.skipUnless((REPO / "pyproject.toml").exists(), "not a repo checkout")
    def test_the_built_wheel_carries_the_skill_files(self) -> None:
        # Non-Python package data is the part a build backend can silently
        # drop; if it does, every install past this checkout dispatches agents
        # that are briefed to use a skill nothing put on the box.
        with tempfile.TemporaryDirectory() as out:
            subprocess.run(
                ["uv", "build", "--wheel", "--offline", "-o", out],
                cwd=REPO,
                check=True,
                capture_output=True,
            )
            wheel = next(Path(out).glob("*.whl"))
            names = zipfile.ZipFile(wheel).namelist()
        for skill in skills.packaged():
            self.assertIn(f"foregent/skills/{skill.name}/SKILL.md", names)


class SkillsRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, clear=False))
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_defaults_to_the_home_claude_directory(self) -> None:
        self.assertEqual(skills.skills_root(), Path.home() / ".claude" / "skills")

    def test_follows_a_relocated_claude_config_directory(self) -> None:
        # A box that sets CLAUDE_CONFIG_DIR has no ~/.claude for Claude Code
        # to read, so installing there would install nowhere.
        os.environ["CLAUDE_CONFIG_DIR"] = "/etc/claude"
        self.assertEqual(skills.skills_root(), Path("/etc/claude/skills"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "skills"

    def worker(self) -> Path:
        return self.root / "foregent-worker" / "SKILL.md"

    def test_setup_installs_into_an_empty_directory(self) -> None:
        outcomes = dict(skills.install(self.root))
        self.assertEqual(outcomes["foregent-worker"], skills.Outcome.INSTALLED)
        self.assertTrue(self.worker().is_file())

    def test_setup_is_idempotent(self) -> None:
        # Re-running after an upgrade must be safe, and must say plainly that
        # it changed nothing rather than reporting a fresh install.
        skills.install(self.root)
        outcomes = dict(skills.install(self.root))
        self.assertEqual(outcomes["foregent-worker"], skills.Outcome.UNCHANGED)

    def test_setup_updates_a_stale_skill_and_says_so(self) -> None:
        # It is foregent's file and setup exists to refresh it, but an
        # operator whose edits were replaced should be told.
        skills.install(self.root)
        self.worker().write_text("stale\n")
        outcomes = dict(skills.install(self.root))
        self.assertEqual(outcomes["foregent-worker"], skills.Outcome.UPDATED)
        self.assertNotEqual(self.worker().read_text(), "stale\n")

    def test_ensure_writes_a_missing_skill(self) -> None:
        self.assertEqual(skills.ensure(self.root), ["foregent-worker"])
        self.assertTrue(self.worker().is_file())

    def test_ensure_leaves_an_existing_skill_alone(self) -> None:
        # This runs on every dispatch; clobbering a deliberately edited skill
        # mid-flight is worse than serving a stale one.
        skills.install(self.root)
        self.worker().write_text("hand edited\n")
        self.assertEqual(skills.ensure(self.root), [])
        self.assertEqual(self.worker().read_text(), "hand edited\n")

    def test_installing_leaves_no_staging_files_behind(self) -> None:
        # The copy is staged in the destination directory to make the rename
        # atomic; a stray temp file there is one Claude Code would try to load.
        skills.install(self.root)
        self.assertEqual(
            [p.name for p in (self.root / "foregent-worker").iterdir()],
            ["SKILL.md"],
        )

    def test_an_empty_directory_does_not_count_as_installed(self) -> None:
        # A half-finished copy must not make the safety net skip the skill.
        (self.root / "foregent-worker").mkdir(parents=True)
        self.assertEqual(skills.ensure(self.root), ["foregent-worker"])
        self.assertTrue(self.worker().is_file())


class SetupCommandTests(unittest.TestCase):
    def test_setup_installs_and_reports(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["setup"]), 0)
        self.assertIn("foregent-worker", out.getvalue())
        self.assertIn("installed", out.getvalue())
        self.assertTrue((config / "skills" / "foregent-worker" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
