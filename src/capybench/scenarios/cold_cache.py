"""Scenario - what a buffer miss costs, and how much of the working set is really cached.

Every other latency number in this suite is a cache hit. The interesting question for a
platform with disaggregated storage - or a noisy node, or a stingy tier - is what happens
when the data is *not* in shared_buffers. That is the path a report query, a cold
endpoint, or a fresh replica takes.

Two tables of ``size_mb`` are created. Scanning the second evicts the first from
shared_buffers, so the next scan of the first is a genuine buffer miss; the scan after it
is warm. The ratio is ``cache_miss_penalty_x``.

The measurement checks itself: ``pg_statio_user_tables`` block counters are read around
each scan, so ``cold_hit_ratio_pct`` reports how much of the "cold" scan really came from
buffers. If eviction did not work (``size_mb`` too small for this target's
shared_buffers), the hit ratio says so rather than the run quietly reporting a fake
penalty.

Honest limit: a buffer miss is not proof of a disk read. The page may still be served by
the OS page cache or ZFS ARC, so this is the *floor* of the storage penalty, not the true
device latency. A host probe's ARC hit/miss delta over the same window is what settles
that - which is exactly why host-vantage readings exist.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

import psycopg

from ..config import Suite, Target
from ..stats import percentile
from ..store import Run, Sample

SCENARIO = "cold_cache"

_TABLE_A = "capybench_cold_a"
_TABLE_B = "capybench_cold_b"
#: Row width chosen so a row is ~128 bytes on disk including tuple overhead.
_PAYLOAD_BYTES = 96


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.cold_cache
    if cfg is None:
        log("cold_cache: no [cold_cache] config; skipping")
        return

    for name in cfg.targets or tuple(suite.targets):
        target = suite.target(name)
        try:
            _measure_target(run, target, size_mb=cfg.size_mb, repeats=cfg.repeats, log=log)
        except psycopg.Error as exc:
            log(f"cold_cache: target {name} FAILED, continuing with next: {exc}")


def _measure_target(
    run: Run,
    target: Target,
    *,
    size_mb: int,
    repeats: int,
    log: Callable[[str], None],
) -> None:
    rows = max(size_mb * 1024 * 1024 // 128, 1000)
    with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=60) as conn:
        conn.prepare_threshold = None
        try:
            log(f"cold_cache: {target.name} seeding 2 x {size_mb} MB ({rows:,} rows each)")
            _seed(conn, _TABLE_A, rows)
            _seed(conn, _TABLE_B, rows)
            _disable_parallel_scan(conn)

            table_bytes = _table_bytes(conn, _TABLE_A)
            cold: list[tuple[float, float]] = []
            warm: list[tuple[float, float]] = []
            for i in range(repeats):
                _scan(conn, _TABLE_B)  # evict A
                cold.append(_timed_scan(conn, _TABLE_A))
                warm.append(_timed_scan(conn, _TABLE_A))
                log(
                    f"cold_cache: {target.name} repeat {i + 1}/{repeats} "
                    f"cold {cold[-1][0]:.0f}ms (hit {cold[-1][1]:.0f}%) / "
                    f"warm {warm[-1][0]:.0f}ms (hit {warm[-1][1]:.0f}%)"
                )
        finally:
            _cleanup(conn)

    _record(run, target, table_bytes, cold, warm, size_mb=size_mb, log=log)


def _seed(conn: psycopg.Connection, table: str, rows: int) -> None:
    conn.execute(f"drop table if exists {table}")
    conn.execute(f"create table {table} (id bigint primary key, payload text not null)")
    conn.execute(
        f"insert into {table} select g, repeat('x', %s) from generate_series(1, %s) g",
        (_PAYLOAD_BYTES, rows),
    )
    conn.execute(f"analyze {table}")


def _cleanup(conn: psycopg.Connection) -> None:
    for table in (_TABLE_A, _TABLE_B):
        # Best-effort: both tables are namespaced, and a failed cleanup must not mask
        # the measurement error that likely caused it.
        with contextlib.suppress(psycopg.Error):
            conn.execute(f"drop table if exists {table}")


def _disable_parallel_scan(conn: psycopg.Connection) -> None:
    """Keep scans single-threaded so the timing compares like with like.

    Best-effort: some platforms refuse the GUC, in which case parallel workers apply
    equally to the cold and warm scan and the ratio still holds.
    """
    with contextlib.suppress(psycopg.Error):
        conn.execute("set max_parallel_workers_per_gather = 0")


def _table_bytes(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute("select pg_total_relation_size(%s)", (table,)).fetchone()
    return int(row[0]) if row else 0


def _block_counters(conn: psycopg.Connection, table: str) -> tuple[int, int]:
    conn.execute("select pg_stat_clear_snapshot()")
    row = conn.execute(
        """
        select coalesce(heap_blks_read, 0), coalesce(heap_blks_hit, 0)
        from pg_statio_user_tables where relname = %s
        """,
        (table,),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _scan(conn: psycopg.Connection, table: str) -> None:
    conn.execute(f"select count(*) from {table}").fetchone()


def _timed_scan(conn: psycopg.Connection, table: str) -> tuple[float, float]:
    """Scan ``table``; return (elapsed_ms, buffer hit ratio during that scan)."""
    read_before, hit_before = _block_counters(conn, table)
    started = time.perf_counter()
    _scan(conn, table)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    read_after, hit_after = _block_counters(conn, table)

    read = read_after - read_before
    hit = hit_after - hit_before
    ratio = 100.0 * hit / (hit + read) if (hit + read) else 0.0
    return elapsed_ms, ratio


def _record(
    run: Run,
    target: Target,
    table_bytes: int,
    cold: list[tuple[float, float]],
    warm: list[tuple[float, float]],
    *,
    size_mb: int,
    log: Callable[[str], None],
) -> None:
    def sample(metric: str, value: float, unit: str, variant: str) -> None:
        run.record(
            Sample(
                scenario=SCENARIO,
                target=target.name,
                target_tier_usd=target.tier_usd,
                pg_version=target.pg_version,
                region=target.region,
                dataset_bytes=table_bytes,
                variant=variant,
                metric=metric,
                value=value,
                unit=unit,
                params={"size_mb": size_mb, "repeats": len(cold)},
            )
        )

    if not cold or not warm:
        return

    cold_ms = percentile([c[0] for c in cold], 50)
    warm_ms = percentile([w[0] for w in warm], 50)
    cold_hit = percentile([c[1] for c in cold], 50)
    warm_hit = percentile([w[1] for w in warm], 50)
    mb = table_bytes / (1024 * 1024) if table_bytes else 0.0

    sample("scan_ms", cold_ms, "ms", "cold")
    sample("scan_ms", warm_ms, "ms", "warm")
    sample("buffer_hit_ratio_pct", cold_hit, "pct", "cold")
    sample("buffer_hit_ratio_pct", warm_hit, "pct", "warm")
    if cold_ms:
        sample("scan_throughput_mb_s", mb / (cold_ms / 1000.0), "mb_s", "cold")
    if warm_ms:
        sample("scan_throughput_mb_s", mb / (warm_ms / 1000.0), "mb_s", "warm")
    if warm_ms:
        sample("cache_miss_penalty_x", cold_ms / warm_ms, "x", "derived")

    if cold_hit > 50.0:
        log(
            f"cold_cache: WARNING {target.name} 'cold' scan still hit buffers "
            f"{cold_hit:.0f}% of the time - size_mb={size_mb} is too small to evict this "
            f"target's shared_buffers, so cache_miss_penalty_x understates the real cost"
        )
    log(
        f"cold_cache: {target.name} cold {cold_ms:.0f}ms vs warm {warm_ms:.0f}ms "
        f"({cold_ms / warm_ms:.1f}x) over {mb:.0f} MB"
    )
