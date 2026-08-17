"""Scenario - how long from "create me a database" to a query answered.

This is the first number a developer experiences and one nobody publishes: sign-up flows
demo it, but nothing measures it repeatedly. It also matters operationally - a CI job that
creates a database per pipeline pays this cost on every run.

Timed in two parts, because they fail differently:

* ``provision_api_ms``    - the control plane accepting the request and handing back real
  credentials.
* ``provision_ready_ms``  - the whole path, request to first successful ``SELECT 1``. The
  difference is how long the platform spends bringing storage and Postgres up after it has
  already answered the API call.

``destroy_ms`` is recorded too: teardown speed decides whether ephemeral databases are
practical in CI at all.

Needs :attr:`~capybench.capabilities.Capability.PROVISION`. Unlike every other scenario
this one has no target to start from - it names a ``[providers.<name>]`` block directly,
since the whole point is creating a database that did not exist.

Each created database is destroyed in a ``finally``: an interrupted run must not leave
billable resources behind.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .. import providers
from ..config import ProvisionSpeedConfig, Suite
from ..store import Run, Sample
from ._naming import run_token

SCENARIO = "provision_speed"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.provision_speed
    if cfg is None:
        log("provision_speed: no [provision_speed] config; skipping")
        return

    provider = providers.by_name(suite, cfg.provider)
    token = run_token(run.run_id)

    for i in range(cfg.repeats):
        name = f"capybench-prov-{i}-{token}"
        log(f"provision_speed: creating {name} (iter {i + 1}/{cfg.repeats})")

        started = time.monotonic()
        target = provider.provision(name, pg_version=cfg.pg_version, region=cfg.region)
        api_ms = (time.monotonic() - started) * 1000.0
        try:
            connect_wait_s = providers.wait_connectable(target, timeout_s=600.0)
            ready_ms = (time.monotonic() - started) * 1000.0

            for metric, value in (
                ("provision_api_ms", api_ms),
                ("provision_ready_ms", ready_ms),
            ):
                _record(run, cfg, name, i, metric, value, connect_wait_s)
            log(f"provision_speed: {name} ready in {ready_ms:.0f} ms (api {api_ms:.0f} ms)")
        finally:
            destroy_started = time.monotonic()
            provider.destroy(target)
            destroy_ms = (time.monotonic() - destroy_started) * 1000.0
            _record(run, cfg, name, i, "destroy_ms", destroy_ms, None)
            log(f"provision_speed: {name} destroyed in {destroy_ms:.0f} ms")


def _record(
    run: Run,
    cfg: ProvisionSpeedConfig,
    name: str,
    repeat: int,
    metric: str,
    value: float,
    connect_wait_s: float | None,
) -> None:
    # The provider block names the platform under test; the created databases are
    # ephemeral, so charting them individually would produce one series per repeat.
    params: dict[str, object] = {"database": name}
    if connect_wait_s is not None:
        params["connect_wait_s"] = round(connect_wait_s, 3)
    run.record(
        Sample(
            scenario=SCENARIO,
            target=cfg.provider,
            target_tier_usd=cfg.tier_usd,
            pg_version=cfg.pg_version,
            region=cfg.region,
            variant=f"repeat-{repeat}",
            metric=metric,
            value=value,
            unit="ms",
            params=params,
        )
    )
