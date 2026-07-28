"""Provisioning the machine's shared MCP servers (JIM-93).

Agents inherit Linear and GitHub from the box rather than carrying them in a
launch spec, so ``foregent setup`` putting them there is what makes an agent
able to read its own issue. The `claude` CLI is stubbed throughout: these
tests are about foregent's decisions — what to write, and when to leave an
operator's own configuration alone — not about Claude Code's writer.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foregent import cli, mcp_servers


class DefinitionTests(unittest.TestCase):
    """The server definitions themselves."""

    def test_no_credential_is_ever_written_to_disk(self) -> None:
        # The stored header holds `${VAR}`, expanded per session by Claude
        # Code. A definition carrying a real token would put it in a
        # world-readable config file that gets copied around.
        for name, definition in mcp_servers.SERVERS.items():
            for value in definition.get("headers", {}).values():
                self.assertRegex(value, r"\$\{\w+\}", f"{name} inlines a secret")

    def test_every_server_names_the_credential_it_needs(self) -> None:
        self.assertEqual(mcp_servers.credentials("linear"), ["LINEAR_API_KEY"])
        self.assertEqual(mcp_servers.credentials("github"), ["GITHUB_TOKEN"])


class ConfigFileTests(unittest.TestCase):
    """Finding the file `--scope user` writes to."""

    def test_the_default_sits_beside_the_config_directory(self) -> None:
        # Not inside it: `~/.claude.json` is a sibling of `~/.claude/`, so
        # this cannot be derived from the skills root.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(mcp_servers.config_file(), Path.home() / ".claude.json")

    def test_claude_config_dir_relocates_it(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/box/cfg"}):
            self.assertEqual(mcp_servers.config_file(), Path("/box/cfg/.claude.json"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})
        )
        self.claude = self.enterContext(
            mock.patch.object(
                mcp_servers.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            )
        )

    def write_config(self, servers: dict) -> None:
        (self.config / ".claude.json").write_text(json.dumps({"mcpServers": servers}))

    def added(self) -> dict[str, dict]:
        """The name -> definition of each server the stub was asked to add."""
        added = {}
        for call in self.claude.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[:3], ["claude", "mcp", "add-json"])
            # User scope is the only one that applies in a fresh per-issue
            # workspace, which is every workspace an agent gets.
            self.assertEqual(argv[-2:], ["-s", "user"])
            added[argv[3]] = json.loads(argv[4])
        return added

    def test_a_fresh_box_gets_every_server(self) -> None:
        outcomes = mcp_servers.install()
        self.assertEqual(self.added(), dict(mcp_servers.SERVERS))
        self.assertTrue(all(installed for _, installed in outcomes))

    def test_an_existing_server_is_left_alone(self) -> None:
        # Re-adding would discard an OAuth login foregent cannot recreate, and
        # setup is meant to be safe to re-run after every upgrade.
        self.write_config({"linear": {"type": "http", "url": "http://operator"}})
        outcomes = mcp_servers.install()
        self.assertEqual(list(self.added()), ["github"])
        self.assertEqual(dict(outcomes), {"linear": False, "github": True})

    def test_an_unreadable_config_is_treated_as_empty(self) -> None:
        # A fresh box has no config at all; half-written JSON should not stop
        # setup, because the add that follows reports the real failure.
        (self.config / ".claude.json").write_text("{not json")
        mcp_servers.install()
        self.assertEqual(sorted(self.added()), ["github", "linear"])

    def test_a_failed_add_is_reported_not_swallowed(self) -> None:
        self.claude.return_value = subprocess.CompletedProcess([], 1, "", "no such flag")
        with self.assertRaises(mcp_servers.MCPError) as caught:
            mcp_servers.install()
        self.assertIn("no such flag", str(caught.exception))

    def test_a_missing_claude_cli_is_reported_not_swallowed(self) -> None:
        self.claude.side_effect = FileNotFoundError("claude")
        with self.assertRaises(mcp_servers.MCPError):
            mcp_servers.install()


class CredentialTests(unittest.TestCase):
    """A configured server whose variable is unset authenticates with nothing."""

    def test_unset_variables_are_reported(self) -> None:
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "GITHUB_TOKEN": ""}):
            self.assertEqual(mcp_servers.missing_credentials(), ["GITHUB_TOKEN"])

    def test_a_fully_credentialed_environment_reports_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k", "GITHUB_TOKEN": "t"}):
            self.assertEqual(mcp_servers.missing_credentials(), [])


class SetupCommandTests(unittest.TestCase):
    """`foregent setup` provisions skills and MCP servers together (JIM-93)."""

    def setUp(self) -> None:
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": str(self.config),
                    "LINEAR_API_KEY": "k",
                    "GITHUB_TOKEN": "t",
                },
            )
        )

    def setup(self, install: mock.Mock) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mcp_servers, "install", install):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["setup"])
        return code, out.getvalue(), err.getvalue()

    def test_setup_installs_the_servers_and_says_so(self) -> None:
        code, out, _ = self.setup(mock.Mock(return_value=[("linear", True)]))
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        self.assertIn("linear", out)
        # The skills half still runs: one command provisions the whole box.
        self.assertIn("foregent-worker", out)

    def test_setup_fails_loudly_when_a_server_cannot_be_added(self) -> None:
        code, _, err = self.setup(
            mock.Mock(side_effect=mcp_servers.MCPError("claude is not installed"))
        )
        self.assertEqual(code, 1)
        self.assertIn("claude is not installed", err)

    def test_setup_warns_about_a_credential_the_box_lacks(self) -> None:
        # Configured but unauthenticated is the failure an agent only finds
        # once it is already working an issue, so say it at setup time.
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            code, _, err = self.setup(mock.Mock(return_value=[("github", True)]))
        self.assertEqual(code, 0)
        self.assertIn("GITHUB_TOKEN", err)


if __name__ == "__main__":
    unittest.main()
