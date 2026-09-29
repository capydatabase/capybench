"""Unit tests for the run loop's glue in capybench.cli."""

from __future__ import annotations

import pytest

from capybench import cli
from capybench.hostprobe.base import HostProbeError


class FailingProbe:
    """A host probe whose every sample fails - the node went unreachable."""

    label = "node"

    def sample(self) -> dict:
        raise HostProbeError("ssh: connection refused")


class ClosingSampleFails:
    """The opening sample works, the closing one does not."""

    label = "node"

    def __init__(self) -> None:
        self.calls = 0

    def sample(self) -> dict:
        self.calls += 1
        if self.calls > 1:
            raise HostProbeError("ssh: connection refused")
        return {}


def test_a_failed_scenario_is_not_hidden_by_a_failed_closing_sample() -> None:
    """A `return` in the window's finally used to swallow the scenario's exception."""
    with (
        pytest.raises(RuntimeError, match="scenario broke"),
        cli._host_window(ClosingSampleFails(), run=None, scenario="s"),
    ):
        raise RuntimeError("scenario broke")


def test_a_probe_failure_never_fails_the_scenario() -> None:
    ran = []
    with cli._host_window(FailingProbe(), run=None, scenario="s"):
        ran.append(True)
    with cli._host_window(ClosingSampleFails(), run=None, scenario="s"):
        ran.append(True)
    assert ran == [True, True]
