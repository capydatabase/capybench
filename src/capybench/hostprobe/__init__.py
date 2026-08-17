"""Host probes: the privileged vantage point on the platform's own infrastructure.

A suite declares at most one ``[host_probe]`` block - the node the targets run on. The
runner snapshots it around every scenario and records deltas with ``vantage = host``,
next to (never mixed with) the client-vantage numbers.

Built-in types:

* ``ssh`` - standard Linux interfaces (PSI, cgroup v2, ZFS arcstats) over one SSH round
  trip; provider-neutral, so any operator can use it.

To add one, subclass :class:`HostProbe`, implement ``facts`` and ``sample``, and register
the class below.
"""

from __future__ import annotations

from ..config import HostProbeConfig
from .base import HostProbe, HostProbeError, Reading, deltas
from .ssh import SSHHostProbe

_REGISTRY: dict[str, type[HostProbe]] = {
    SSHHostProbe.type: SSHHostProbe,
}


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def create(config: HostProbeConfig) -> HostProbe:
    """Instantiate the probe a ``[host_probe]`` block describes."""
    cls = _REGISTRY.get(config.type)
    if cls is None:
        raise ValueError(
            f"unknown host_probe type {config.type!r}; "
            f"registered types: {', '.join(registered_types())}"
        )
    return cls(config.name, config.settings)


__all__ = [
    "HostProbe",
    "HostProbeError",
    "Reading",
    "SSHHostProbe",
    "create",
    "deltas",
    "registered_types",
]
