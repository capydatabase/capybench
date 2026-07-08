"""Scenario 3 — branch/preview creation speed vs parent size.

ZFS copy-on-write clones are O(constant) regardless of how large the parent is. Create
branches against progressively larger parents and time each one end-to-end (control
command issued → branch answers ``SELECT 1``). The expected chart is a flat CapyDB line
against competitors whose snapshot-restore time climbs with data size.

Requires ``control.<provider>.commands.branch_create`` to be configured. Growing the
parent to each target size is provider-specific and left to the operator (see the note in
``fill_parent`` below); wire it up per environment before trusting the large-size points.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..config import Suite
from ..control import commands
from ..store import Run, Sample

SCENARIO = "branch_speed"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.branch_speed
    if cfg is None:
        log("branch_speed: no [branch_speed] config; skipping")
        return

    parent = suite.target(cfg.target)
    control = suite.control_for(parent)

    for size_mb in cfg.parent_sizes_mb:
        log(f"branch_speed: ensuring parent {parent.name} ≈ {size_mb} MB")
        fill_parent(parent, size_mb, log=log)

        for i in range(cfg.repeats):
            branch_name = f"capybench-{size_mb}mb-{i}"
            log(f"branch_speed: creating branch {branch_name}")
            wall_start = time.monotonic()
            branch = commands.create_branch(control, parent, branch_name)
            connect_wait = commands.wait_connectable(branch)
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
            commands.delete_branch(control, parent, branch_name)


def fill_parent(target, size_mb: int, *, log: Callable[[str], None]) -> None:
    """Ensure the parent DB holds roughly ``size_mb`` of data.

    Uses ``generate_series`` into a filler table sized to the target. This is a portable
    default; if a provider bills or caps storage differently, adjust per environment. The
    branch-create time is what matters, so exactness of the fill is not critical — only
    that parents differ by size across the sweep.
    """
    import psycopg

    target_bytes = size_mb * 1024 * 1024
    with psycopg.connect(target.dsn(), autocommit=True) as conn:
        conn.execute(
            "create table if not exists capybench_filler "
            "(id bigserial primary key, payload text)"
        )
        current = conn.execute(
            "select pg_total_relation_size('capybench_filler')"
        ).fetchone()
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
