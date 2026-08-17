"""Scenario 4 - scale-to-zero cold-start / wake latency.

Put the instance to sleep via the provider, wait for it to settle, then time the first
connection + ``SELECT 1``. On scale-to-zero platforms that first connect is what wakes
the instance, so this directly measures wake-from-sleep as a client experiences it.
Repeat for a distribution (charts show the CDF).

Needs :attr:`~capybench.capabilities.Capability.SLEEP`; the runner skips this scenario for
providers without it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from .. import providers
from ..config import Suite
from ..store import Run, Sample

SCENARIO = "cold_start"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.cold_start
    if cfg is None:
        log("cold_start: no [cold_start] config; skipping")
        return

    target = suite.target(cfg.target)
    provider = providers.for_target(suite, target)

    for i in range(cfg.repeats):
        log(f"cold_start: sleeping {target.name} (iter {i + 1}/{cfg.repeats})")
        provider.trigger_sleep(target)
        _busy_wait(cfg.settle_s)

        started = time.monotonic()
        with psycopg.connect(target.dsn(), connect_timeout=60) as conn:
            conn.execute("select 1")
        cold_ms = (time.monotonic() - started) * 1000.0

        run.record(
            Sample(
                scenario=SCENARIO,
                target=cfg.target,
                target_tier_usd=target.tier_usd,
                pg_version=target.pg_version,
                region=target.region,
                variant=f"iter-{i}",
                metric="cold_start_ms",
                value=cold_ms,
                unit="ms",
                params={"settle_s": cfg.settle_s},
            )
        )
        log(f"cold_start: wake #{i + 1} = {cold_ms:.0f} ms")

        # A warm follow-up connect quantifies the cold penalty for the same instance.
        warm_start = time.monotonic()
        with psycopg.connect(target.dsn(), connect_timeout=10) as conn:
            conn.execute("select 1")
        warm_ms = (time.monotonic() - warm_start) * 1000.0
        run.record(
            Sample(
                scenario=SCENARIO,
                target=cfg.target,
                target_tier_usd=target.tier_usd,
                variant=f"iter-{i}",
                metric="warm_connect_ms",
                value=warm_ms,
                unit="ms",
            )
        )


def _busy_wait(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
