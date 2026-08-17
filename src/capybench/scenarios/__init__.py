"""Benchmark scenarios. Each module exposes ``run(suite, run, *, log)``.

This package is also the registry. Every scenario declares the provider
:class:`~capybench.capabilities.Capability` set it needs, and the runner resolves and
checks that *once, here* - so a scenario body never re-implements "is this supported?"
and the skip message reads the same everywhere. Most scenarios need no capability at all:
they only need a DSN, which is why they run against ``generic`` targets - i.e. against
any managed Postgres, including competitors.

Order matters and is fixed below: cheap read-only observation first, sustained load
after, and anything that disturbs the target (restoring, provisioning, sleeping) last.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from types import ModuleType
from typing import Literal, Protocol

from ..capabilities import Capability, describe
from ..config import Suite
from ..store import Run
from . import (
    branch_speed,
    cold_cache,
    cold_start,
    connection_ceiling,
    fact_sheet,
    noisy_neighbor,
    provision_speed,
    query_latency,
    restore_rto,
    sustained,
    throughput,
    vacuum_under_write,
)

Log = Callable[[str], None]
Scope = Literal["none", "target", "provider"]
#: Wraps each scenario - used to snapshot a host probe around it (see ``capybench.cli``).
Around = Callable[[str], AbstractContextManager[None]]


class ScenarioRun(Protocol):
    def __call__(self, suite: Suite, run: Run, *, log: Log) -> None: ...


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """One registered scenario: what it needs and how to reach it."""

    name: str
    run: ScenarioRun
    #: Attribute on :class:`Suite` holding this scenario's config (``None`` = not configured).
    config_attr: str
    requires: frozenset[Capability] = frozenset()
    #: Whether the capability is checked against a target's provider or a named provider.
    scope: Scope = "none"
    #: Config attribute naming that target / provider.
    resolves: str | None = None


def _spec(
    name: str,
    module: ModuleType,
    *,
    requires: Iterable[Capability] = (),
    scope: Scope = "none",
    resolves: str | None = None,
) -> ScenarioSpec:
    return ScenarioSpec(
        name=name,
        run=module.run,
        config_attr=name,
        requires=frozenset(requires),
        scope=scope,
        resolves=resolves,
    )


SPECS: tuple[ScenarioSpec, ...] = (
    # -- observation: cheap, read-mostly, safe to run first ------------------------------
    _spec("fact_sheet", fact_sheet),
    _spec("query_latency", query_latency),
    _spec("connection_ceiling", connection_ceiling),
    _spec("cold_cache", cold_cache),
    # -- load -----------------------------------------------------------------------------
    _spec("throughput", throughput),
    _spec("noisy_neighbor", noisy_neighbor),
    _spec("vacuum_under_write", vacuum_under_write),
    _spec("sustained", sustained),
    # -- lifecycle: disturbs the target, so it goes last ---------------------------------
    _spec(
        "branch_speed",
        branch_speed,
        requires=[Capability.BRANCH],
        scope="target",
        resolves="target",
    ),
    _spec(
        "restore_rto",
        restore_rto,
        requires=[Capability.RESTORE],
        scope="target",
        resolves="target",
    ),
    _spec(
        "provision_speed",
        provision_speed,
        requires=[Capability.PROVISION],
        scope="provider",
        resolves="provider",
    ),
    _spec(
        "cold_start",
        cold_start,
        requires=[Capability.SLEEP],
        scope="target",
        resolves="target",
    ),
)

NAMES: tuple[str, ...] = tuple(scenario.name for scenario in SPECS)

_BY_NAME: dict[str, ScenarioSpec] = {scenario.name: scenario for scenario in SPECS}


def spec(name: str) -> ScenarioSpec:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(NAMES)}") from exc


def unsupported_reason(suite: Suite, scenario: ScenarioSpec) -> str | None:
    """Why ``scenario`` cannot run against this suite, or ``None`` if it can.

    Resolving a provider can itself fail (unknown type, bad settings); that is reported
    as a reason too, so a misconfigured block degrades to a clear skip instead of a
    traceback mid-run.
    """
    from .. import providers  # local import: providers imports config, not scenarios

    if not scenario.requires:
        return None
    cfg = getattr(suite, scenario.config_attr)
    if cfg is None or scenario.resolves is None:
        return None

    ref = getattr(cfg, scenario.resolves)
    try:
        if scenario.scope == "provider":
            provider = providers.by_name(suite, ref)
            subject = f"provider {ref!r}"
        else:
            provider = providers.for_target(suite, suite.target(ref))
            subject = f"target {ref!r}"
    except (KeyError, ValueError, providers.ProviderError) as exc:
        return f"[{scenario.name}] {exc}"

    missing = scenario.requires - provider.capabilities
    if not missing:
        return None
    return (
        f"[{scenario.name}] {subject} uses provider {provider.name!r} "
        f"(type={provider.type}), which supports {describe(provider.capabilities)} - "
        f"needs {describe(missing)}"
    )


def run_all(
    suite: Suite,
    run: Run,
    *,
    only: set[str],
    log: Log,
    around: Around | None = None,
) -> list[str]:
    """Run every configured, supported scenario in ``only``. Returns the names executed.

    A scenario with no config block is skipped silently (that is how you choose what to
    run); an unsupported one is skipped loudly, because a missing capability is something
    the reader of the results needs to know about.

    ``around`` wraps each executed scenario, which is how host-vantage readings are
    captured for exactly the window a scenario occupied.

    A scenario that raises is reported and the matrix continues. A full run can take
    hours; losing the remaining scenarios because one target went away mid-sweep would
    throw away work that is already good, and the failure stays visible in the log either
    way. Samples recorded before the failure are already committed.
    """
    executed: list[str] = []
    for scenario in SPECS:
        if scenario.name not in only:
            continue
        if getattr(suite, scenario.config_attr) is None:
            continue
        reason = unsupported_reason(suite, scenario)
        if reason is not None:
            log(f"skipping {reason}")
            continue
        try:
            with around(scenario.name) if around else nullcontext():
                scenario.run(suite, run, log=log)
        # Reported, never swallowed - see the docstring for why the matrix continues.
        except Exception as exc:
            log(f"scenario {scenario.name} FAILED, continuing with the rest: {exc!r}")
            continue
        executed.append(scenario.name)
    return executed


__all__ = ["NAMES", "SPECS", "ScenarioSpec", "run_all", "spec", "unsupported_reason"]
