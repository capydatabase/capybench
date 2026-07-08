"""Scenario 2 — noisy-neighbor isolation (the strongest differentiator).

Measure a victim workload's tail latency twice: (a) alone, and (b) while an aggressor
hammers a *second* project on the same host. CapyDB's cgroup + per-dataset isolation
should keep the victim's p99 nearly flat; a shared-tenant setup degrades it. The recorded
``degradation_pct`` is the headline number.

Point ``victim`` and ``aggressor`` at two projects co-located on the same physical host to
make the test meaningful.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Suite
from ..runners import pgbench, sysbench
from ..store import Run, Sample

SCENARIO = "noisy_neighbor"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.noisy_neighbor
    if cfg is None:
        log("noisy_neighbor: no [noisy_neighbor] config; skipping")
        return

    victim = suite.target(cfg.victim)
    aggressor = suite.target(cfg.aggressor)

    log(f"noisy_neighbor: preparing victim {victim.name} and aggressor {aggressor.name}")
    sysbench.prepare(victim)
    pgbench.initialize(aggressor, scale=100)

    try:
        # (a) Victim alone — the clean baseline.
        log(f"noisy_neighbor: victim-alone p{cfg.sysbench_percentile}")
        alone = sysbench.run(
            victim,
            threads=cfg.victim_concurrency,
            duration_s=cfg.duration_s,
            percentile=cfg.sysbench_percentile,
        )
        _record(run, suite, cfg.victim, "victim-alone", alone, cfg.victim_concurrency)

        # (b) Victim while the aggressor storms the co-located project.
        log(f"noisy_neighbor: starting aggressor c={cfg.aggressor_concurrency}")
        proc = pgbench.spawn(
            aggressor,
            clients=cfg.aggressor_concurrency,
            duration_s=cfg.duration_s + 10,  # outlast the victim measurement
        )
        try:
            log(f"noisy_neighbor: victim-under-attack p{cfg.sysbench_percentile}")
            attacked = sysbench.run(
                victim,
                threads=cfg.victim_concurrency,
                duration_s=cfg.duration_s,
                percentile=cfg.sysbench_percentile,
            )
        finally:
            proc.terminate()
            proc.wait(timeout=30)
        _record(run, suite, cfg.victim, "victim-under-attack", attacked, cfg.victim_concurrency)

        # Headline: how much did the tail move?
        pct = int(cfg.sysbench_percentile)
        degradation = (attacked.latency_pct_ms / alone.latency_pct_ms - 1.0) * 100.0
        t = suite.target(cfg.victim)
        run.record(
            Sample(
                scenario=SCENARIO,
                target=cfg.victim,
                target_tier_usd=t.tier_usd,
                pg_version=t.pg_version,
                region=t.region,
                concurrency=cfg.victim_concurrency,
                variant="degradation",
                metric=f"p{pct}_degradation_pct",
                value=degradation,
                unit="percent",
                params={"aggressor": cfg.aggressor, "aggressor_c": cfg.aggressor_concurrency},
            )
        )
        log(f"noisy_neighbor: p{pct} tail moved {degradation:+.1f}% under load")
    finally:
        sysbench.cleanup(victim)


def _record(
    run: Run,
    suite: Suite,
    target: str,
    variant: str,
    r: sysbench.SysbenchResult,
    concurrency: int,
) -> None:
    t = suite.target(target)
    common = dict(
        scenario=SCENARIO,
        target=target,
        target_tier_usd=t.tier_usd,
        pg_version=t.pg_version,
        region=t.region,
        concurrency=concurrency,
        variant=variant,
    )
    run.record(
        Sample(
            metric=f"p{r.percentile}_latency_ms",
            value=r.latency_pct_ms,
            unit="ms",
            raw={"stdout": r.stdout},
            **common,
        )
    )
    run.record(Sample(metric="tps", value=r.tps, unit="tps", **common))
