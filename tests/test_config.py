"""Tests for the environment-overridable settings (JIM-98)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
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


class StateFileTests(unittest.TestCase):
    """Where the issue store is persisted (JIM-249)."""

    def test_the_environment_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_STATE_FILE": "/var/lib/fg.json"}):
            self.assertEqual(config.state_file(), Path("/var/lib/fg.json"))

    def test_the_default_is_under_the_home_state_directory(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": "/home/box"}, clear=True):
            self.assertEqual(
                config.state_file(),
                Path("/home/box/.local/state/foregent/state.json"),
            )

    def test_an_empty_variable_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_STATE_FILE": "", "HOME": "/home/box"}):
            self.assertEqual(
                config.state_file(),
                Path("/home/box/.local/state/foregent/state.json"),
            )


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


class MaxActiveTests(unittest.TestCase):
    """How many agents a box will run at once, versus merely hold (JIM-248)."""

    def test_the_environment_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_MAX_ACTIVE": "5"}):
            self.assertEqual(config.max_active(), 5)

    def test_the_default_is_used_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.max_active(), config.DEFAULT_MAX_ACTIVE)

    def test_unset_uses_its_own_default_not_max_agents(self) -> None:
        # The two knobs are tuned separately: raising FOREGENT_MAX_AGENTS
        # does not, on its own, also raise how many run at once (JIM-248).
        with mock.patch.dict(os.environ, {"FOREGENT_MAX_AGENTS": "7"}, clear=True):
            self.assertEqual(config.max_active(), config.DEFAULT_MAX_ACTIVE)

    def test_a_value_that_is_not_a_number_falls_back_and_says_so(self) -> None:
        with mock.patch.dict(
            os.environ, {"FOREGENT_MAX_ACTIVE": "three", "FOREGENT_MAX_AGENTS": "4"}
        ):
            with self.assertLogs(config.logger, "WARNING"):
                self.assertEqual(config.max_active(), config.DEFAULT_MAX_ACTIVE)

    def test_no_setting_can_stop_every_wake(self) -> None:
        # A zero would leave the box refusing every wake with nothing to say
        # why, which is worse than ignoring the operator.
        for setting in ("0", "-1"):
            with self.subTest(setting=setting):
                with mock.patch.dict(os.environ, {"FOREGENT_MAX_ACTIVE": setting}):
                    self.assertEqual(config.max_active(), 1)


class LogLevelTests(unittest.TestCase):
    """What level the server logs at (JIM-149)."""

    def test_the_environment_overrides_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_LOG_LEVEL": "debug"}):
            self.assertEqual(config.log_level(), "debug")

    def test_the_default_is_used_when_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.log_level(), config.DEFAULT_LOG_LEVEL)

    def test_a_level_is_read_however_it_is_spelled(self) -> None:
        with mock.patch.dict(os.environ, {"FOREGENT_LOG_LEVEL": " DEBUG "}):
            self.assertEqual(config.log_level(), "debug")

    def test_a_value_that_is_not_a_level_falls_back_and_says_so(self) -> None:
        # A typo in a systemd unit should not be what stops the bridge from
        # starting, so it warns rather than raising.
        with mock.patch.dict(os.environ, {"FOREGENT_LOG_LEVEL": "verbose"}):
            with self.assertLogs(config.logger, "WARNING"):
                self.assertEqual(config.log_level(), config.DEFAULT_LOG_LEVEL)


if __name__ == "__main__":
    unittest.main()
