"""Tests for the environment-overridable settings (JIM-98)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from foregent import config


class HerdrSessionTests(unittest.TestCase):
    """Which herdr session the bridge runs agents in."""

    def test_the_explicit_variable_wins(self) -> None:
        # First in the order on purpose: the systemd unit sets it, and runs
        # outside any herdr pane.
        with mock.patch.dict(
            os.environ,
            {"FOREGENT_HERDR_SESSION": "foregent", "HERDR_SOCKET_PATH": "/tmp/x.sock"},
        ):
            self.assertEqual(config.herdr_session(), "foregent")

    def test_nothing_set_defers_to_the_client(self) -> None:
        # None is what makes herdr.socket_path's own resolution reachable:
        # this process's session, then herdr's default.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(config.herdr_session())

    def test_an_empty_variable_is_not_a_session_name(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_HERDR_SESSION": ""}):
            self.assertIsNone(config.herdr_session())


class ApiUrlTests(unittest.TestCase):
    def test_the_environment_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_API_URL": "http://box:9000"}):
            self.assertEqual(config.api_url(), "http://box:9000")

    def test_the_default_is_used_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.api_url(), config.DEFAULT_API_URL)


class MaxAgentsTests(unittest.TestCase):
    """How many agents a box will run at once (JIM-151)."""

    def test_the_environment_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_MAX_AGENTS": "5"}):
            self.assertEqual(config.max_agents(), 5)

    def test_the_default_is_used_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.max_agents(), config.DEFAULT_MAX_AGENTS)

    def test_a_value_that_is_not_a_number_falls_back_and_says_so(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_MAX_AGENTS": "three"}):
            with self.assertLogs(config.logger, "WARNING"):
                self.assertEqual(config.max_agents(), config.DEFAULT_MAX_AGENTS)

    def test_no_setting_can_stop_dispatch_altogether(self) -> None:
        # A zero would leave the box refusing to dispatch with nothing to say
        # why, which is worse than ignoring the operator.
        for setting in ("0", "-1"):
            with self.subTest(setting=setting):
                with mock.patch.dict(os.environ, {"FOREGENT_MAX_AGENTS": setting}):
                    self.assertEqual(config.max_agents(), 1)


if __name__ == "__main__":
    unittest.main()
