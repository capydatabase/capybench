"""Scenario 3 - branch/preview creation speed vs parent size.

Copy-on-write branching (e.g. ZFS clones) is O(constant) regardless of how large the
parent is. Create branches against progressively larger parents and time each one
end-to-end (provider call issued -> branch answers ``SELECT 1``). The expected chart is a
flat line for CoW providers against snapshot-restore designs whose time climbs with data
size.

Needs :attr:`~capybench.capabilities.Capability.BRANCH`; the runner skips this scenario
for providers without it. Growing the parent to each target size uses a portable
``generate_series`` filler (see ``fill_parent``); adjust per environment if a platform
bills or caps storage differently before trusting the large-size points.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .. import providers
from ..config import Suite, Target
from ..store import Run, Sample
from ._naming import run_token

SCENARIO = "branch_speed"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.branch_speed
    if cfg is None:
        log("branch_speed: no [branch_speed] config; skipping")
        return

    parent = suite.target(cfg.target)
    provider = providers.for_target(suite, parent)

    for size_mb in cfg.parent_sizes_mb:
        log(f"branch_speed: ensuring parent {parent.name} ≈ {size_mb} MB")
        fill_parent(parent, size_mb, log=log)

        for i in range(cfg.repeats):
            branch_name = _branch_name(run.run_id, size_mb, i)
            log(f"branch_speed: creating branch {branch_name}")
            wall_start = time.monotonic()
            branch = provider.create_branch(parent, branch_name)
            try:
                connect_wait = providers.wait_connectable(branch)
                total_ms = (time.monotonic() - wall_start) * 1000.0

                run.record(
                    Sample(
                        scenario=SCENARIO,
                        target=cfg.target,
                        target_tier_usd=parent.tier_usd,
                        pg_version=parent.pg_version,
                        region=parent.region,
                        dataset_bytes=size_mb * 1024 * 1024,
                        variant=f"repeat-{i}",
                        metric="branch_create_ms",
                        value=total_ms,
                        unit="ms",
                        params={"connect_wait_s": round(connect_wait, 3)},
                    )
                )
                log(f"branch_speed: {branch_name} ready in {total_ms:.0f} ms")
            finally:
                provider.delete_branch(parent, branch_name)


def _branch_name(run_id: str, size_mb: int, repeat: int) -> str:
    """A run-unique preview name so stale previews cannot fake a fast result."""
    return f"capybench-{size_mb}mb-{repeat}-{run_token(run_id)}"


def fill_parent(target: Target, size_mb: int, *, log: Callable[[str], None]) -> None:
    """Ensure the parent DB holds roughly ``size_mb`` of data.

    Uses ``generate_series`` into a filler table sized to the target. This is a portable
    default; if a provider bills or caps storage differently, adjust per environment. The
    branch-create time is what matters, so exactness of the fill is not critical - only
    that parents differ by size across the sweep.
    """
    import psycopg

    target_bytes = size_mb * 1024 * 1024
    with psycopg.connect(target.dsn(), autocommit=True) as conn:
        conn.execute(
            "create table if not exists capybench_filler (id bigserial primary key, payload text)"
        )
        current = conn.execute("select pg_total_relation_size('capybench_filler')").fetchone()
        have = int(current[0]) if current else 0
        if have >= target_bytes:
            return
        # ~1 KiB per row; insert in batches until we reach the target size.
        missing_rows = max(0, (target_bytes - have) // 1024)
        log(f"branch_speed: filling ~{missing_rows} rows into parent")
        batch = 100_000
        remaining = missing_rows
        while remaining > 0:
            n = min(batch, remaining)
            conn.execute(
                "insert into capybench_filler (payload) "
                "select repeat('x', 1024) from generate_series(1, %s)",
                (n,),
            )
            remaining -= n
