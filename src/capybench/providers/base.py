"""Provider adapter base class.

A :class:`Provider` is the harness's window into the platform behind a target. The
interface is derived from exactly what the scenarios use - nothing speculative. Each
operation is gated by a :class:`~capybench.capabilities.Capability` the adapter must
advertise in :attr:`Provider.capabilities`:

===================  ==========================  ==============================================
Capability           Methods                     Scenario
===================  ==========================  ==============================================
``PROVISION``        ``provision`` / ``destroy`` ``provision_speed``
``BRANCH``           ``create_branch`` /         ``branch_speed``
                     ``delete_branch``
``SLEEP``            ``trigger_sleep``           ``cold_start``
``RESTORE``          ``restore`` (cleaned up     ``restore_rto``
                     with ``delete_branch``)
===================  ==========================  ==============================================

Scenarios that need only a connectable DSN (``throughput``, ``query_latency``,
``fact_sheet``, ``connection_ceiling``, ``sustained``, ``vacuum_under_write``,
``cold_cache``, ``noisy_neighbor``) require no capability at all and therefore run
against every provider, including ``generic``.

There is deliberately no wake hook: the first client connection after sleep *is* the
wake, and timing that connection is the measurement itself.
"""

from __future__ import annotations

import time
from abc import ABC
from collections.abc import Mapping
from typing import ClassVar

from ..capabilities import Capability
from ..config import Target


class ProviderError(RuntimeError):
    """A provider lifecycle operation failed, is unsupported, or is misconfigured."""


class Provider(ABC):
    """Adapter for one managed-Postgres platform.

    Subclasses set ``type`` (the registry key used by ``[providers.<name>] type = ...``),
    advertise the capabilities they genuinely implement, and override the matching
    methods. The defaults describe a plain connect-and-measure Postgres with no lifecycle
    control.
    """

    type: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, name: str, settings: Mapping[str, object]) -> None:
        self.name = name
        self.settings: dict[str, object] = dict(settings)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    # -- Capability.PROVISION ------------------------------------------------------------

    def provision(self, name: str) -> Target:
        """Create a brand-new database and return connectable coordinates.

        The harness times request -> ``SELECT 1``, so implementations must return only
        once the platform has handed back real credentials; waiting for connectability is
        the caller's job (see :func:`wait_connectable`).
        """
        raise self._unsupported("database provisioning")

    def destroy(self, target: Target) -> None:
        """Destroy a database created by :meth:`provision`."""
        raise self._unsupported("database destruction")

    # -- Capability.BRANCH ---------------------------------------------------------------

    def create_branch(self, parent: Target, branch_name: str) -> Target:
        """Create a copy-on-write branch of ``parent``; return a connectable Target."""
        raise self._unsupported("branch creation")

    def delete_branch(self, parent: Target, branch_name: str) -> None:
        """Delete a branch previously created with :meth:`create_branch` or :meth:`restore`."""
        raise self._unsupported("branch deletion")

    # -- Capability.SLEEP ----------------------------------------------------------------

    def trigger_sleep(self, target: Target) -> None:
        """Force the instance behind ``target`` to sleep (scale-to-zero).

        Waking is not modeled: the next client connection wakes the instance, and the
        cold_start scenario times exactly that.
        """
        raise self._unsupported("forced sleep")

    # -- Capability.RESTORE --------------------------------------------------------------

    def restore(self, target: Target, name: str, *, restore_time: str | None = None) -> Target:
        """Restore ``target`` into a *fresh* database and return connectable coordinates.

        ``restore_time`` is an RFC 3339 timestamp; ``None`` means "the most recent
        recoverable state". Restoring must never overwrite the source - the harness
        measures recovery time objective, and a benchmark that can destroy production is
        not a benchmark anyone will run. The result is cleaned up with
        :meth:`delete_branch`, so implementations should return something that method
        accepts.
        """
        raise self._unsupported("restore")

    # -- internals -----------------------------------------------------------------------

    def _unsupported(self, operation: str) -> ProviderError:
        return ProviderError(
            f"provider {self.name!r} (type={self.type}) does not support {operation}"
        )


def wait_connectable(target: Target, *, timeout_s: float = 120.0, poll_s: float = 0.5) -> float:
    """Block until ``SELECT 1`` succeeds against ``target``. Returns seconds waited.

    Used after branch/provision/restore to measure end-to-end "request -> connectable".
    """
    import psycopg  # imported lazily so providers have no hard psycopg dep at import time

    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            with psycopg.connect(target.dsn(), connect_timeout=5) as conn:
                conn.execute("select 1")
            return time.monotonic() - started
        except psycopg.Error as exc:
            last_err = exc
            time.sleep(poll_s)
    raise ProviderError(f"{target.name} never became connectable: {last_err}")
