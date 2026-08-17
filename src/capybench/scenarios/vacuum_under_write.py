"""Scenario - does autovacuum keep up, and what does bloat do to latency?

A short benchmark never meets Postgres's actual steady-state problem: under sustained
writes, dead tuples accumulate and autovacuum has to win the race. When it loses, tables
grow without bound and latency drifts upward long after throughput still looks fine. How
aggressively a platform tunes autovacuum - and whether its IO budget lets vacuum finish -
is a real difference between managed offerings that no TPS chart shows.

A write workload runs for ``duration_s`` while this process samples, every
``sample_interval_s``:

* dead / live tuples across the pgbench tables, and the dead-tuple ratio
* total relation bytes - is the table growing faster than vacuum reclaims?
* accumulated autovacuum runs
* a point-query latency probe, to catch latency drifting while throughput does not

The headline metrics are ``dead_tuple_pct_max`` (did vacuum ever fall behind?),
``table_growth_pct`` (did the loss become permanent?) and ``latency_drift_pct``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from ..config import Suite, Target
from ..runners import pgbench
from ..store import Run, Sample

SCENARIO = "vacuum_under_write"

_STATS_SQL = """
select coalesce(sum(s.n_dead_tup), 0)::bigint,
       coalesce(sum(s.n_live_tup), 0)::bigint,
       coalesce(sum(s.autovacuum_count), 0)::bigint,
       coalesce(sum(pg_total_relation_size(s.relid)), 0)::bigint
from pg_stat_user_tables s
where s.relname like 'pgbench\\_%'
"""


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.vacuum_under_write
    if cfg is None:
        log("vacuum_under_write: no [vacuum_under_write] config; skipping")
        return

    target = suite.target(cfg.target)
    log(f"vacuum_under_write: initializing pgbench on {target.name} (scale={cfg.scale})")
    try:
        pgbench.initialize(target, scale=cfg.scale)
    except RuntimeError as exc:
        log(f"vacuum_under_write: init FAILED on {target.name}: {exc}")
        return

    log(
        f"vacuum_under_write: {cfg.duration_s}s of writes at {cfg.concurrency} clients, "
        f"sampling every {cfg.sample_interval_s}s"
    )
    writer = pgbench.spawn(target, clients=cfg.concurrency, duration_s=cfg.duration_s)
    try:
        series = _sample_loop(
            target,
            duration_s=cfg.duration_s,
            interval_s=cfg.sample_interval_s,
            still_running=lambda: writer.poll() is None,
            log=log,
        )
    finally:
        _, stderr = writer.communicate()

    sustained_tps = pgbench.parse_progress_tps(stderr)
    if not sustained_tps:
        raise RuntimeError(
            f"vacuum_under_write: the write load on {target.name} produced no progress "
            f"output - the workload did not run, so the vacuum measurements are "
            f"meaningless:\n{stderr[-500:]}"
        )

    _record(run, target, cfg.concurrency, series, sustained_tps, log=log)


def _sample_loop(
    target: Target,
    *,
    duration_s: int,
    interval_s: int,
    still_running: Callable[[], bool],
    log: Callable[[str], None],
) -> list[dict[str, float]]:
    series: list[dict[str, float]] = []
    started = time.monotonic()
    with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=30) as conn:
        conn.prepare_threshold = None
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= duration_s or not still_running():
                break
            try:
                series.append(_snapshot(conn, elapsed))
            except psycopg.Error as exc:
                log(f"vacuum_under_write: sample at {elapsed:.0f}s failed: {exc}")
            time.sleep(min(interval_s, max(duration_s - (time.monotonic() - started), 0.1)))
    return series


def _snapshot(conn: psycopg.Connection, elapsed_s: float) -> dict[str, float]:
    row = conn.execute(_STATS_SQL).fetchone()
    dead, live, autovacuums, total_bytes = row if row else (0, 0, 0, 0)

    probe_started = time.perf_counter()
    conn.execute("select count(*) from pgbench_branches").fetchone()
    probe_ms = (time.perf_counter() - probe_started) * 1000.0

    return {
        "elapsed_s": elapsed_s,
        "dead_tuples": float(dead),
        "live_tuples": float(live),
        "dead_tuple_pct": 100.0 * dead / (dead + live) if (dead + live) else 0.0,
        "table_bytes": float(total_bytes),
        "autovacuum_count": float(autovacuums),
        "probe_latency_ms": probe_ms,
    }


def _record(
    run: Run,
    target: Target,
    concurrency: int,
    series: list[dict[str, float]],
    sustained_tps: list[float],
    *,
    log: Callable[[str], None],
) -> None:
    def sample(metric: str, value: float, unit: str, **kwargs: object) -> None:
        run.record(
            Sample(
                scenario=SCENARIO,
                target=target.name,
                target_tier_usd=target.tier_usd,
                pg_version=target.pg_version,
                region=target.region,
                concurrency=concurrency,
                metric=metric,
                value=value,
                unit=unit,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    if not series:
        log(f"vacuum_under_write: no samples collected on {target.name}")
        return

    units = {
        "dead_tuples": "tuples",
        "live_tuples": "tuples",
        "dead_tuple_pct": "pct",
        "table_bytes": "bytes",
        "autovacuum_count": "count",
        "probe_latency_ms": "ms",
    }
    for point in series:
        params = {"elapsed_s": point["elapsed_s"]}
        for metric, unit in units.items():
            sample(metric, point[metric], unit, variant="window", params=params)

    first, last = series[0], series[-1]
    growth = (
        100.0 * (last["table_bytes"] - first["table_bytes"]) / first["table_bytes"]
        if first["table_bytes"]
        else 0.0
    )
    latency_drift = (
        100.0 * (last["probe_latency_ms"] - first["probe_latency_ms"]) / first["probe_latency_ms"]
        if first["probe_latency_ms"]
        else 0.0
    )
    shape = {
        "dead_tuple_pct_max": (max(p["dead_tuple_pct"] for p in series), "pct"),
        "dead_tuple_pct_final": (last["dead_tuple_pct"], "pct"),
        "table_growth_pct": (growth, "pct"),
        "latency_drift_pct": (latency_drift, "pct"),
        "autovacuum_runs": (last["autovacuum_count"] - first["autovacuum_count"], "count"),
        "write_tps_avg": (sum(sustained_tps) / len(sustained_tps), "tps"),
    }
    for metric, (value, unit) in shape.items():
        sample(metric, value, unit, variant="shape")

    log(
        f"vacuum_under_write: {target.name} dead-tuple peak "
        f"{shape['dead_tuple_pct_max'][0]:.1f}%, table +{growth:.1f}%, "
        f"{int(shape['autovacuum_runs'][0])} autovacuum run(s), "
        f"latency drift {latency_drift:+.1f}%"
    )
