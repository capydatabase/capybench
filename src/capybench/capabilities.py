"""Provider capabilities - the vocabulary scenarios use to declare what they need.

A :class:`Capability` names one lifecycle operation a managed-Postgres platform may
expose. Providers advertise the set they genuinely implement; scenarios declare the set
they require. The runner intersects the two and skips - with an explanatory message -
rather than calling an unsupported operation.

Capabilities exist only when a scenario actually calls them. Adding a member without a
scenario that needs it is how an interface rots into stubs, and a provider that claims a
capability it cannot deliver corrupts published comparisons.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """One lifecycle operation a provider may implement."""

    #: Create and destroy a throwaway database on demand (``provision_speed``).
    PROVISION = "provision"
    #: Create and delete a branch / preview copy of a database (``branch_speed``).
    BRANCH = "branch"
    #: Force scale-to-zero so the next connection is a cold start (``cold_start``).
    SLEEP = "sleep"
    #: Restore recent state into a fresh connectable database (``restore_rto``).
    RESTORE = "restore"


def describe(capabilities: frozenset[Capability]) -> str:
    """Render a capability set for logs and ``capybench check`` output."""
    return ", ".join(sorted(capabilities)) if capabilities else "none"
