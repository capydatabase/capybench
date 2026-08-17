"""Generic provider: any Postgres reachable by DSN, with no lifecycle control.

This is the default for targets that do not name a provider. It advertises no
capabilities, which is exactly right: there is no portable way to branch, force-sleep,
provision or restore arbitrary Postgres. Every scenario that needs only a connectable
DSN still runs against it - which is most of them.
"""

from __future__ import annotations

from .base import Provider


class GenericProvider(Provider):
    """Plain Postgres DSN. No lifecycle capabilities - and no settings needed."""

    type = "generic"
