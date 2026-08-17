"""Host-probe adapter base class - the privileged (``vantage = host``) vantage point.

A :class:`HostProbe` reads what a client connection structurally cannot see: how much CPU
the cgroup was throttled, whether the node was under memory or IO pressure, what the
cache hit ratio was. Only the operator of a platform can run one, and that is the point -
it is how a provider publishes ground truth alongside numbers a customer can reproduce.

The runner snapshots the probe around every scenario and records the delta, so a claim
like "the victim's p99 doubled" can be set next to "the cgroup was throttled for 4.1 s
during that window".

Two kinds of reading, because they aggregate differently:

* **counters** monotonically increase; the runner reports the delta across a scenario.
* **gauges** are point-in-time; the runner reports the before and after values.

Host readings are never mixed onto a client-vantage chart axis.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar


class HostProbeError(RuntimeError):
    """A host probe could not collect readings."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One host measurement."""

    value: float
    unit: str
    #: True for monotonic counters (report deltas), False for point-in-time gauges.
    counter: bool = False


class HostProbe(ABC):
    """Adapter that reads node-level metrics from the platform's own infrastructure."""

    type: ClassVar[str]

    def __init__(self, name: str, settings: Mapping[str, object]) -> None:
        self.name = name
        self.settings: dict[str, object] = dict(settings)

    @property
    def label(self) -> str:
        """Node identifier recorded as the ``target`` of host-vantage rows."""
        return self.name

    @abstractmethod
    def facts(self) -> dict[str, str]:
        """Static node facts (kernel, CPU count, OS). Recorded once per run."""

    @abstractmethod
    def sample(self) -> dict[str, Reading]:
        """Current readings. Called before and after each scenario."""


def deltas(before: Mapping[str, Reading], after: Mapping[str, Reading]) -> dict[str, Reading]:
    """Counter deltas between two samples.

    Gauges are excluded (they are recorded as before/after instead), as are counters that
    went backwards - a counter reset means the node or cgroup restarted mid-scenario and
    the delta would be a fiction.
    """
    out: dict[str, Reading] = {}
    for key, end in after.items():
        start = before.get(key)
        if start is None or not end.counter or not start.counter:
            continue
        if end.value < start.value:
            continue
        out[key] = Reading(end.value - start.value, end.unit, counter=True)
    return out
