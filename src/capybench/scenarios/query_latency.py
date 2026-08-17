"""Scenario 5 - single-query latency (the "endpoint" number).

OLTP suites report whole-transaction latency (~20 round-trips per transaction), which
understates how fast a single query feels from an application. This scenario measures
what an app endpoint actually experiences, per target:

* ``select-1``      - warm connection, ``SELECT 1``: pure protocol + network round-trip.
* ``point-select``  - warm connection, indexed PK lookup on a seeded table: the typical
  "fetch one row" endpoint query.
* ``fresh-connect`` - full cold path: TCP + TLS + SCRAM handshake + first query. This is
  the number to compare against HTTP data-API gateways (PostgREST-style stacks), where
  every request pays a gateway hop instead.

Each variant records min/avg/p50/p95/p99/max. Auto-prepare is disabled so results are
comparable through transaction-pooling ports (PgBouncer) and represent a simple driver.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import psycopg

from ..config import Suite
from ..stats import summarize_ms
from ..store import Run, Sample

SCENARIO = "query_latency"

_TABLE = "capybench_kv"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.query_latency
    if cfg is None:
        log("query_latency: no [query_latency] config; skipping")
        return

    names = cfg.targets or tuple(suite.targets)
    for name in names:
        target = suite.target(name)
        log(f"query_latency: seeding {_TABLE} on {name} ({cfg.rows} rows)")
        _seed(target.dsn(), cfg.rows)
        try:
            warm = _measure_warm(target.dsn(), repeats=cfg.repeats, rows=cfg.rows)
            connects = _measure_fresh_connects(target.dsn(), repeats=cfg.connect_repeats)
            for variant, latencies in {**warm, "fresh-connect": connects}.items():
                summary = _record(run, suite, name, variant, latencies)
                log(
                    f"query_latency: {name} {variant} "
                    f"p50={summary['p50_ms']:.2f}ms p95={summary['p95_ms']:.2f}ms "
                    f"(n={len(latencies)})"
                )
        finally:
            _cleanup(target.dsn())


def _seed(dsn: str, rows: int) -> None:
    with psycopg.connect(dsn, autocommit=True, connect_timeout=60) as conn:
        conn.execute(f"drop table if exists {_TABLE}")
        conn.execute(f"create table {_TABLE} (id int primary key, payload text not null)")
        conn.execute(
            f"insert into {_TABLE} select g, md5(g::text) from generate_series(1, %s) g",
            (rows,),
        )
        conn.execute(f"analyze {_TABLE}")


def _cleanup(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=30) as conn:
            conn.execute(f"drop table if exists {_TABLE}")
    except psycopg.Error:
        pass  # best-effort; the seeded table is tiny and namespaced


def _measure_warm(dsn: str, *, repeats: int, rows: int) -> dict[str, list[float]]:
    rng = random.Random(42)
    results: dict[str, list[float]] = {"select-1": [], "point-select": []}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=60) as conn:
        conn.prepare_threshold = None  # simple protocol path; pooler-safe

        for _ in range(min(50, repeats)):  # warmup, not recorded
            conn.execute("select 1").fetchone()

        for _ in range(repeats):
            started = time.perf_counter()
            conn.execute("select 1").fetchone()
            results["select-1"].append((time.perf_counter() - started) * 1000.0)

        for _ in range(repeats):
            key = rng.randint(1, rows)
            started = time.perf_counter()
            conn.execute(f"select payload from {_TABLE} where id = %s", (key,)).fetchone()
            results["point-select"].append((time.perf_counter() - started) * 1000.0)
    return results


def _measure_fresh_connects(dsn: str, *, repeats: int) -> list[float]:
    latencies: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        with psycopg.connect(dsn, connect_timeout=60) as conn:
            conn.execute("select 1").fetchone()
        latencies.append((time.perf_counter() - started) * 1000.0)
    return latencies


def _record(
    run: Run,
    suite: Suite,
    target_name: str,
    variant: str,
    latencies: list[float],
) -> dict[str, float]:
    target = suite.target(target_name)
    summary = summarize_ms(latencies)
    for metric, value in summary.items():
        run.record(
            Sample(
                scenario=SCENARIO,
                target=target_name,
                target_tier_usd=target.tier_usd,
                pg_version=target.pg_version,
                region=target.region,
                concurrency=1,
                variant=variant,
                metric=metric,
                value=value,
                unit="ms",
                params={"n": len(latencies), "auto_prepare": False},
            )
        )
    return summary
