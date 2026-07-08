"""Scenario 1 — raw OLTP throughput & latency baseline.

For every target, sweep client concurrency and record TPS + tail latency at each point.
pgbench provides the recognizable TPS number; sysbench provides tail latency (p95/p99).
This is the table-stakes "we're not slower at the same price" chart — compare targets at
matching ``tier_usd``.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Suite
from ..runners import pgbench, sysbench
from ..store import Run, Sample

SCENARIO = "throughput"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.throughput
    if cfg is None:
        log("throughput: no [throughput] config; skipping")
        return

    for target in suite.targets.values():
        log(f"throughput: initializing pgbench on {target.name} (scale={cfg.scale})")
        pgbench.initialize(target, scale=cfg.scale)
        sysbench.prepare(target)

        try:
            for clients in cfg.concurrencies:
                log(f"throughput: {target.name} pgbench c={clients}")
                pg = pgbench.run(target, clients=clients, duration_s=cfg.duration_s)
                _record_pg(run, suite, target.name, clients, pg, variant="read-write")

                log(f"throughput: {target.name} sysbench c={clients}")
                sb = sysbench.run(
                    target,
                    threads=clients,
                    duration_s=cfg.duration_s,
                    percentile=cfg.sysbench_percentile,
                )
                _record_sb(run, suite, target.name, clients, sb, variant="read-write")

                if cfg.read_only:
                    pg_ro = pgbench.run(
                        target, clients=clients, duration_s=cfg.duration_s, read_only=True
                    )
                    _record_pg(run, suite, target.name, clients, pg_ro, variant="read-only")
        finally:
            sysbench.cleanup(target)


def _record_pg(
    run: Run,
    suite: Suite,
    target: str,
    clients: int,
    r: pgbench.PgbenchResult,
    *,
    variant: str,
) -> None:
    t = suite.target(target)
    common = dict(
        scenario=SCENARIO,
        target=target,
        target_tier_usd=t.tier_usd,
        pg_version=t.pg_version,
        region=t.region,
        concurrency=clients,
        variant=variant,
        params={"tool": "pgbench", "duration_s": None},
    )
    run.record(
        Sample(metric="tps", value=r.tps, unit="tps", raw={"stdout": r.stdout}, **common)
    )
    run.record(
        Sample(metric="latency_avg_ms", value=r.latency_avg_ms, unit="ms", **common)
    )


def _record_sb(
    run: Run,
    suite: Suite,
    target: str,
    clients: int,
    r: sysbench.SysbenchResult,
    *,
    variant: str,
) -> None:
    t = suite.target(target)
    common = dict(
        scenario=SCENARIO,
        target=target,
        target_tier_usd=t.tier_usd,
        pg_version=t.pg_version,
        region=t.region,
        concurrency=clients,
        variant=variant,
        params={"tool": "sysbench"},
    )
    run.record(
        Sample(
            metric="tps_sysbench", value=r.tps, unit="tps", raw={"stdout": r.stdout}, **common
        )
    )
    run.record(
        Sample(
            metric=f"p{r.percentile}_latency_ms",
            value=r.latency_pct_ms,
            unit="ms",
            **common,
        )
    )
    run.record(
        Sample(
            metric="latency_avg_ms_sysbench", value=r.latency_avg_ms, unit="ms", **common
        )
    )
