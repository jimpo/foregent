"""Tests for the ``foregent`` CLI's own argument handling (JIM-149)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import unittest
from unittest import mock

from foregent import cli


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
