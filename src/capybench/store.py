"""Persistence for benchmark samples and facts.

Thin wrapper over psycopg. A :class:`Run` groups everything from one harness invocation:

* :meth:`Run.record` writes a numeric measurement (``bench_sample``).
* :meth:`Run.record_fact` writes a non-numeric observation - a setting, a version, a
  capability verdict (``bench_fact``). Facts are strings, so forcing them through the
  sample table's ``double precision`` column would lose them.

Every row carries a :class:`Vantage`: measurements taken through a client DSN and
measurements taken with privileged access on the platform's own node answer different
questions and must never share a chart axis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

import psycopg

from . import __version__


class Vantage(StrEnum):
    """Where a measurement was taken from."""

    #: Through a normal client connection - what any customer can reproduce.
    CLIENT = "client"
    #: On the platform's own infrastructure, with privileged access the customer lacks.
    HOST = "host"


@dataclass(slots=True)
class Sample:
    """One numeric measurement, mirroring a ``bench_sample`` row."""

    scenario: str
    target: str
    metric: str
    value: float
    unit: str
    vantage: Vantage = Vantage.CLIENT
    target_tier_usd: float | None = None
    pg_version: str | None = None
    region: str | None = None
    dataset_bytes: int | None = None
    concurrency: int | None = None
    variant: str | None = None
    params: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class Fact:
    """One non-numeric observation about a target, mirroring a ``bench_fact`` row.

    ``source`` records how the value was obtained so a reader can tell a setting that was
    read from ``pg_settings`` apart from one that was proven by probing behavior.
    """

    scenario: str
    target: str
    key: str
    value: str
    source: str = "pg_settings"
    vantage: Vantage = Vantage.CLIENT
    category: str | None = None


class Run:
    """A single harness invocation. Use as a context manager to stamp ``finished_at``."""

    def __init__(self, conn: psycopg.Connection, run_id: str) -> None:
        self._conn = conn
        self.run_id = run_id
        # Which repeat pass is currently executing. Stamped into every sample's
        # params so a run with repeats > 1 yields a DISTRIBUTION rather than a
        # single point. Set by the CLI between passes; scenarios never touch it,
        # which is why repeats needed no change to any scenario.
        self.repeat: int = 0

    @classmethod
    def start(
        cls,
        dsn: str,
        *,
        git_sha: str | None,
        notes: str | None,
        client: dict[str, Any] | None = None,
        attestation: dict[str, Any] | None = None,
    ) -> Run:
        conn = psycopg.connect(dsn, autocommit=True)
        row = conn.execute(
            """
            insert into bench_run (harness_version, git_sha, notes, client, attestation)
            values (%s, %s, %s, %s, %s)
            returning id::text
            """,
            (
                __version__,
                git_sha,
                notes,
                json.dumps(client or {}),
                json.dumps(attestation or {}),
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to create bench_run row")
        return cls(conn, row[0])

    def record(self, sample: Sample) -> None:
        self._conn.execute(
            """
            insert into bench_sample (
                run_id, scenario, target, vantage, target_tier_usd, pg_version, region,
                dataset_bytes, concurrency, variant, metric, value, unit, params, raw
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id,
                sample.scenario,
                sample.target,
                str(sample.vantage),
                sample.target_tier_usd,
                sample.pg_version,
                sample.region,
                sample.dataset_bytes,
                sample.concurrency,
                sample.variant,
                sample.metric,
                sample.value,
                sample.unit,
                json.dumps({**(sample.params or {}), "repeat": self.repeat}),
                json.dumps(sample.raw or {}),
            ),
        )

    def record_fact(self, fact: Fact) -> None:
        self._conn.execute(
            """
            insert into bench_fact (run_id, scenario, target, vantage, category, key, value, source)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.run_id,
                fact.scenario,
                fact.target,
                str(fact.vantage),
                fact.category,
                fact.key,
                fact.value,
                fact.source,
            ),
        )

    def finish(self) -> None:
        self._conn.execute("update bench_run set finished_at = now() where id = %s", (self.run_id,))

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
