"""Provider adapters: one class per managed-Postgres platform.

Built-in types:

* ``generic`` - any Postgres reachable by DSN; connect-and-measure only (the default).
* ``capydb``  - provisioning, preview-database branching, forced sleep, PITR restore.

To add a provider (e.g. for your platform's branching API):

1. Subclass :class:`Provider` in a new module here, set ``type``, and list in
   ``capabilities`` only the :class:`~capybench.capabilities.Capability` members you
   actually implement - then override the matching methods. Read credentials from the
   environment, not the TOML.
2. Register the class in ``_REGISTRY`` below.
3. Reference it from the config: a ``[providers.<name>]`` block with ``type = "<type>"``
   (or name the block after the type), and point targets at it with ``provider = "<name>"``.

Keep the interface honest: only claim a capability the platform genuinely has, so the
scenarios skip cleanly everywhere else.
"""

from __future__ import annotations

from ..capabilities import Capability
from ..config import ProviderConfig, Suite, Target
from .base import Provider, ProviderError, wait_connectable
from .capydb import CapyDBProvider
from .generic import GenericProvider

_REGISTRY: dict[str, type[Provider]] = {
    GenericProvider.type: GenericProvider,
    CapyDBProvider.type: CapyDBProvider,
}


def registered_types() -> tuple[str, ...]:
    """The provider ``type`` keys the registry knows about."""
    return tuple(sorted(_REGISTRY))


def create(config: ProviderConfig) -> Provider:
    """Instantiate the adapter a ``[providers.<name>]`` block describes."""
    cls = _REGISTRY.get(config.type)
    if cls is None:
        raise ValueError(
            f"unknown provider type {config.type!r} for [providers.{config.name}]; "
            f"registered types: {', '.join(registered_types())}"
        )
    return cls(config.name, config.settings)


def for_target(suite: Suite, target: Target) -> Provider:
    """Resolve and instantiate the provider a target references."""
    return create(suite.provider_config(target))


def by_name(suite: Suite, name: str) -> Provider:
    """Resolve a provider by ``[providers.<name>]`` block name (no target involved).

    Used by scenarios that create a database from nothing, so there is no target to
    resolve through yet.
    """
    cfg = suite.providers.get(name)
    if cfg is None:
        known = ", ".join(sorted(suite.providers)) or "(none)"
        raise ValueError(f"provider {name!r} not defined; known [providers.*] blocks: {known}")
    return create(cfg)


__all__ = [
    "Capability",
    "CapyDBProvider",
    "GenericProvider",
    "Provider",
    "ProviderError",
    "by_name",
    "create",
    "for_target",
    "registered_types",
    "wait_connectable",
]
