"""Persistence for benchmark samples.

Thin wrapper over psycopg. A :class:`Run` groups all samples from one harness invocation;
:meth:`Run.record` writes a single measurement in the tall schema defined in
``sql/001_results_schema.sql``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import psycopg

from . import __version__


@dataclass(slots=True)
class Sample:
    """One measurement, mirroring a ``bench_sample`` row."""

    scenario: str
    target: str
    metric: str
    value: float
    unit: str
    target_tier_usd: float | None = None
    pg_version: str | None = None
    region: str | None = None
    dataset_bytes: int | None = None
    concurrency: int | None = None
    variant: str | None = None
    params: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


class Run:
    """A single harness invocation. Use as a context manager to stamp ``finished_at``."""

    def __init__(self, conn: psycopg.Connection, run_id: str) -> None:
        self._conn = conn
        self.run_id = run_id

    @classmethod
    def start(cls, dsn: str, *, git_sha: str | None, notes: str | None) -> Run:
        conn = psycopg.connect(dsn, autocommit=True)
        row = conn.execute(
            """
            insert into bench_run (harness_version, git_sha, notes)
            values (%s, %s, %s)
            returning id::text
            """,
            (__version__, git_sha, notes),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to create bench_run row")
        return cls(conn, row[0])

    def record(self, sample: Sample) -> None:
        self._conn.execute(
            """
            insert into bench_sample (
                run_id, scenario, target, target_tier_usd, pg_version, region,
                dataset_bytes, concurrency, variant, metric, value, unit, params, raw
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id,
                sample.scenario,
                sample.target,
                sample.target_tier_usd,
                sample.pg_version,
                sample.region,
                sample.dataset_bytes,
                sample.concurrency,
                sample.variant,
                sample.metric,
                sample.value,
                sample.unit,
                json.dumps(sample.params or {}),
                json.dumps(sample.raw or {}),
            ),
        )

    def finish(self) -> None:
        self._conn.execute(
            "update bench_run set finished_at = now() where id = %s", (self.run_id,)
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.finish()
        finally:
            self._conn.close()
