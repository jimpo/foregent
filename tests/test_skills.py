"""Checks on the skills foregent ships (JIM-90).

A skill is prose, so there is little to assert about its content — but its
frontmatter is a contract with Claude Code's loader, and a skill that fails to
load fails silently: the agent just works the issue its own way and never
reports back. These guard the parts that must be machine-readable.
"""

from __future__ import annotations

import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "skills"


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


class SkillTests(unittest.TestCase):
    def skills(self) -> list[Path]:
        return sorted(SKILLS.glob("*/SKILL.md"))

    def test_every_skill_is_installable(self) -> None:
        # One SKILL.md per directory is the shape the installer copies and the
        # loader expects.
        self.assertTrue(self.skills(), "no skills found")
        for directory in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
            self.assertTrue(
                (directory / "SKILL.md").exists(),
                f"{directory} has no SKILL.md",
            )

    def test_frontmatter_names_match_their_directories(self) -> None:
        # The loader keys on the declared name; a mismatch makes a skill
        # unfindable under the name everything else refers to.
        for skill in self.skills():
            self.assertEqual(frontmatter(skill)["name"], skill.parent.name)

    def test_every_skill_describes_when_to_use_it(self) -> None:
        # The description is what the model decides to load on.
        for skill in self.skills():
            self.assertGreater(len(frontmatter(skill).get("description", "")), 40)

    def test_the_worker_skill_covers_the_lifecycle_tools(self) -> None:
        # Foregent learns nothing about an issue's outcome except through
        # these, so a skill that omits them leaves the bridge blind.
        body = (SKILLS / "foregent-worker" / "SKILL.md").read_text()
        self.assertIn("complete_task", body)
        self.assertIn("report_blocked", body)


if __name__ == "__main__":
    unittest.main()
