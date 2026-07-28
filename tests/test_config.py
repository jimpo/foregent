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
        # outside any herdr pane (docs/PLAN.md §5.10).
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


if __name__ == "__main__":
    unittest.main()
