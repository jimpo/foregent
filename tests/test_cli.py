"""Tests for the ``foregent`` CLI's own argument handling (JIM-149)."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import signal
import unittest
from unittest import mock

from foregent import cli
from foregent.agents import Provider


class QueueProviderTests(unittest.TestCase):
    """`queue --provider`, the one thing about a dispatch the operator names."""

    def parse(self, *argv: str) -> argparse.Namespace:
        return cli.build_parser().parse_args(["queue", "JIM-42", *argv])

    def test_claude_code_is_the_default(self) -> None:
        self.assertEqual(self.parse().provider, Provider.CLAUDE)

    def test_a_harness_can_be_named(self) -> None:
        self.assertEqual(self.parse("--provider", "codex").provider, "codex")

    def test_a_harness_foregent_cannot_run_is_refused(self) -> None:
        # Refused here rather than at launch, where the issue would already
        # have been claimed in Linear.
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.parse("--provider", "emacs")

    def test_every_provider_is_offered(self) -> None:
        # A harness foregent can run and the CLI will not accept is one nobody
        # can reach.
        for provider in Provider:
            with self.subTest(provider=provider):
                parsed = self.parse("--provider", str(provider))
                self.assertEqual(parsed.provider, provider)

    def test_the_request_names_the_harness(self) -> None:
        # The server decides nothing about which harness works an issue; it is
        # told, and refuses one it cannot run.
        with mock.patch.object(cli.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = io.StringIO(
                json.dumps({"key": "JIM-42", "status": "Queued"})
            )
            cli.main(["queue", "JIM-42", "--provider", "codex"])
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["provider"], "codex")


class ServeLogLevelTests(unittest.TestCase):
    """`serve --log-level`, and the config dict it produces."""

    def parse(self, *argv: str) -> argparse.Namespace:
        return cli.build_parser().parse_args(["serve", *argv])

    def test_the_default_comes_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_LOG_LEVEL": "warning"}):
            self.assertEqual(self.parse().log_level, "warning")

    def test_the_flag_wins_over_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_LOG_LEVEL": "warning"}):
            self.assertEqual(self.parse("--log-level", "debug").log_level, "debug")

    def test_an_unknown_level_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                self.parse("--log-level", "verbose")

    def test_the_level_reaches_the_root_logger(self) -> None:
        # The bridge's own records ride on the root logger; uvicorn configures
        # only its own, so this is what carries them.
        config = cli.serve_log_config("debug")
        self.assertEqual(config["root"], {"handlers": ["default"], "level": "DEBUG"})

    def test_every_accepted_level_is_one_dictconfig_knows(self) -> None:
        # `dictConfig` resolves a level by name, and knows upper case only.
        known = logging.getLevelNamesMapping()
        for level in cli.LOG_LEVELS:
            with self.subTest(level=level):
                self.assertIn(cli.serve_log_config(level)["root"]["level"], known)


class ServeShutdownTests(unittest.TestCase):
    def test_first_signal_starts_a_graceful_shutdown(self) -> None:
        graceful_exit = mock.Mock()
        server = mock.Mock(should_exit=False)

        cli.exit_on_second_signal(graceful_exit)(server, signal.SIGTERM, None)

        graceful_exit.assert_called_once_with(server, signal.SIGTERM, None)

    def test_second_interrupt_or_terminate_signal_stops_immediately(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig):
                graceful_exit = mock.Mock()
                server = mock.Mock(should_exit=True)

                with self.assertRaises(KeyboardInterrupt):
                    cli.exit_on_second_signal(graceful_exit)(server, sig, None)

                self.assertTrue(server.force_exit)
                graceful_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
