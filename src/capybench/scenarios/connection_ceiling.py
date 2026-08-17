"""Scenario - how many connections you really get, and what happens at the wall.

``max_connections`` is a claim. What an application meets is a behavior: at some point
new connections are refused, or silently queued behind a pooler, and the latency of
already-open sessions may degrade long before that. Connection limits are one of the most
common production surprises on managed Postgres and one of the least benchmarked, so this
scenario opens connections one at a time until the platform says no.

Recorded per target:

* ``connect_ms`` per connection ordinal - the ramp curve; a pooler queueing rather than
  refusing shows up here as a latency wall instead of an error.
* ``max_client_connections`` - how many were actually held open at once.
* ``advertised_max_connections`` / ``connection_headroom_pct`` - claim vs reality.
* ``query_at_ceiling_ms`` - latency on the *first* connection while the ceiling is held,
  i.e. what an established session feels when the pool is exhausted.

This scenario deliberately walks a database toward its limit, so ``max_probe`` defaults
to a modest 100 - raise it consciously, and not against production.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from ..config import Suite, Target
from ..stats import summarize_ms
from ..store import Fact, Run, Sample

SCENARIO = "connection_ceiling"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.connection_ceiling
    if cfg is None:
        log("connection_ceiling: no [connection_ceiling] config; skipping")
        return

    for name in cfg.targets or tuple(suite.targets):
        target = suite.target(name)
        try:
            _probe_target(
                suite,
                run,
                target,
                max_probe=cfg.max_probe,
                connect_timeout_s=cfg.connect_timeout_s,
                log=log,
            )
        except psycopg.Error as exc:
            log(f"connection_ceiling: target {name} FAILED, continuing with next: {exc}")


def _probe_target(
    suite: Suite,
    run: Run,
    target: Target,
    *,
    max_probe: int,
    connect_timeout_s: int,
    log: Callable[[str], None],
) -> None:
    open_conns: list[psycopg.Connection] = []
    latencies: list[float] = []
    refusal: str | None = None

    def record(metric: str, value: float, unit: str, *, concurrency: int | None = None) -> None:
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
                params={"max_probe": max_probe},
            )
        )

    try:
        for ordinal in range(1, max_probe + 1):
            started = time.perf_counter()
            try:
                conn = psycopg.connect(
                    target.dsn(), autocommit=True, connect_timeout=connect_timeout_s
                )
            except psycopg.Error as exc:
                refusal = str(exc).strip()[:300]
                break
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            open_conns.append(conn)
            latencies.append(elapsed_ms)
            record("connect_ms", elapsed_ms, "ms", concurrency=ordinal)

        established = len(open_conns)
        cause = "server_refused" if refusal else "harness_cap"

        advertised = _advertised_max(open_conns[0]) if open_conns else None
        query_at_ceiling = _query_latency_ms(open_conns[0]) if open_conns else None
    finally:
        for conn in open_conns:
            conn.close()

    record("max_client_connections", float(established), "connections")
    if latencies:
        summary = summarize_ms(latencies)
        for metric in ("p50_ms", "p95_ms", "max_ms"):
            record(f"connect_{metric}", summary[metric], "ms")
    if query_at_ceiling is not None:
        record("query_at_ceiling_ms", query_at_ceiling, "ms")
    if advertised is not None:
        record("advertised_max_connections", float(advertised), "connections")
        if advertised > 0:
            record("connection_headroom_pct", 100.0 * established / advertised, "pct")

    for key, value in (
        ("ceiling_cause", cause),
        ("ceiling_error", refusal or "-"),
    ):
        run.record_fact(
            Fact(
                scenario=SCENARIO,
                target=target.name,
                key=key,
                value=value,
                source="probe",
                category="connections",
            )
        )

    detail = (
        f"refused after {established}: {refusal}"
        if refusal
        else f"reached the harness cap of {max_probe} without refusal"
    )
    log(f"connection_ceiling: {target.name} held {established} connections - {detail}")


def _advertised_max(conn: psycopg.Connection) -> int | None:
    row = conn.execute("select current_setting('max_connections')").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _query_latency_ms(conn: psycopg.Connection) -> float:
    started = time.perf_counter()
    conn.execute("select 1").fetchone()
    return (time.perf_counter() - started) * 1000.0
