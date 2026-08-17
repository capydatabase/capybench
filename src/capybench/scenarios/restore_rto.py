"""Scenario - recovery time objective: how long until restored data answers a query.

Every managed-Postgres platform advertises backups. Almost none publish how long a
restore takes, and buyers discover it during an incident. This scenario measures it the
only way that means anything: request a restore, then time until the restored database
answers ``SELECT 1``.

``restore_rto_ms`` is the whole path - control-plane job, storage materialization, Postgres
recovery, first connection. The restore is measured, not simulated, so the number includes
whatever queueing the platform imposes on a real restore.

Needs :attr:`~capybench.capabilities.Capability.RESTORE`. The provider contract requires
restoring into a *fresh* database, never over the source: a benchmark that can destroy
production is one nobody will run twice. Cleanup happens in a ``finally``.

Note on comparability: a restore's duration depends on how much data and WAL there is to
replay, so publish the source database's size alongside the number. Across platforms, only
compare restores of similar-sized databases.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import psycopg

from .. import providers
from ..config import Suite, Target
from ..store import Run, Sample
from ._naming import run_token

SCENARIO = "restore_rto"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.restore_rto
    if cfg is None:
        log("restore_rto: no [restore_rto] config; skipping")
        return

    target = suite.target(cfg.target)
    provider = providers.for_target(suite, target)
    token = run_token(run.run_id)
    source_bytes = _database_bytes(target, log=log)

    for i in range(cfg.repeats):
        name = f"capybench-restore-{i}-{token}"
        when = cfg.restore_time or "most recent recoverable state"
        log(f"restore_rto: restoring {target.name} -> {name} ({when})")

        started = time.monotonic()
        restored = provider.restore(target, name, restore_time=cfg.restore_time)
        job_ms = (time.monotonic() - started) * 1000.0
        try:
            connect_wait_s = providers.wait_connectable(restored, timeout_s=900.0)
            total_ms = (time.monotonic() - started) * 1000.0

            for metric, value in (
                ("restore_job_ms", job_ms),
                ("restore_rto_ms", total_ms),
            ):
                run.record(
                    Sample(
                        scenario=SCENARIO,
                        target=cfg.target,
                        target_tier_usd=target.tier_usd,
                        pg_version=target.pg_version,
                        region=target.region,
                        dataset_bytes=source_bytes,
                        variant=f"repeat-{i}",
                        metric=metric,
                        value=value,
                        unit="ms",
                        params={
                            "restore_time": cfg.restore_time or "latest",
                            "connect_wait_s": round(connect_wait_s, 3),
                        },
                    )
                )
            log(f"restore_rto: {name} answered a query {total_ms:.0f} ms after the request")
        finally:
            provider.delete_branch(target, name)


def _database_bytes(target: Target, *, log: Callable[[str], None]) -> int | None:
    """Size of the source database - a restore time is meaningless without it."""
    try:
        with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=30) as conn:
            row = conn.execute("select pg_database_size(current_database())").fetchone()
    except psycopg.Error as exc:
        log(f"restore_rto: could not read source database size: {exc}")
        return None
    return int(row[0]) if row else None
