"""Scenario - capability & posture fact sheet (no load, no numbers).

The most important thing to publish next to a throughput figure is the configuration that
produced it. A platform that wins a TPS chart with ``fsync = off`` is not beating anyone;
it is answering a different question. This scenario records, per target, the posture that
makes every other measurement in the run comparable - and, as a side effect, the
capability matrix buyers actually shop on (which extensions exist, what a role may do,
whether a pooler is in the path).

Facts are grouped by ``category`` and tagged with a ``source``:

* ``pg_settings`` - read from the catalog; what the platform *claims*.
* ``probe``       - proven by behavior; what the platform *does*. Where the two can
  disagree (connection routing, GUC permissions), the probe is the honest answer.

Everything here is read-only and cheap: a handful of queries per target, no load, no DDL
outside a rolled-back transaction.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from ..config import Suite, Target
from ..store import Fact, Run

SCENARIO = "fact_sheet"

#: Settings that change what a benchmark number *means*, or what the database can do.
_SETTINGS: dict[str, str] = {
    # Durability: the fairness-critical block.
    "fsync": "durability",
    "synchronous_commit": "durability",
    "full_page_writes": "durability",
    "wal_level": "durability",
    "wal_compression": "durability",
    "data_checksums": "durability",
    "max_wal_size": "durability",
    "checkpoint_timeout": "durability",
    # Resources: what the tier actually bought.
    "shared_buffers": "resources",
    "effective_cache_size": "resources",
    "work_mem": "resources",
    "maintenance_work_mem": "resources",
    "max_connections": "resources",
    "superuser_reserved_connections": "resources",
    "max_worker_processes": "resources",
    "max_parallel_workers": "resources",
    "max_parallel_workers_per_gather": "resources",
    "huge_pages": "resources",
    # Planner / IO shape.
    "random_page_cost": "planner",
    "effective_io_concurrency": "planner",
    "jit": "planner",
    "default_toast_compression": "planner",
    "track_io_timing": "planner",
    # Operational policy the platform imposes on you.
    "autovacuum": "policy",
    "statement_timeout": "policy",
    "idle_in_transaction_session_timeout": "policy",
    "lock_timeout": "policy",
    "shared_preload_libraries": "policy",
    "timezone": "policy",
}

#: Extensions worth knowing about when choosing a platform (catalog names).
_NOTABLE_EXTENSIONS = (
    "vector",
    "postgis",
    "pg_cron",
    "pg_stat_statements",
    "timescaledb",
    "pgaudit",
    "pg_partman",
    "pg_trgm",
    "pgcrypto",
    "hstore",
    "uuid-ossp",
    "postgres_fdw",
    "pg_repack",
    "hypopg",
    "http",
)

#: GUCs an application realistically wants to set per session.
_PROBE_GUCS = (
    "statement_timeout",
    "lock_timeout",
    "idle_in_transaction_session_timeout",
    "work_mem",
    "search_path",
    "synchronous_commit",
    "jit",
    "default_transaction_isolation",
    "application_name",
    "timezone",
)


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.fact_sheet
    if cfg is None:
        log("fact_sheet: no [fact_sheet] config; skipping")
        return

    for name in cfg.targets or tuple(suite.targets):
        target = suite.target(name)
        try:
            facts = collect(target, probe_gucs=cfg.probe_gucs)
        except psycopg.Error as exc:
            log(f"fact_sheet: target {name} FAILED, continuing with next: {exc}")
            continue
        for fact in facts:
            run.record_fact(fact)
        posture = next((f.value for f in facts if f.key == "durability_posture"), "unknown")
        log(f"fact_sheet: {name} recorded {len(facts)} facts (durability: {posture})")


def collect(target: Target, *, probe_gucs: bool = True) -> list[Fact]:
    """Every fact for one target, in one connection."""
    facts: list[Fact] = []

    def add(key: str, value: object, *, category: str, source: str = "pg_settings") -> None:
        facts.append(
            Fact(
                scenario=SCENARIO,
                target=target.name,
                key=key,
                value=str(value),
                source=source,
                category=category,
            )
        )

    with psycopg.connect(target.dsn(), autocommit=True, connect_timeout=30) as conn:
        conn.prepare_threshold = None  # pooler-safe

        settings = _settings(conn)
        for key, value in settings.items():
            add(key, value, category=_SETTINGS.get(key, "other"))

        posture, note = durability_posture(settings)
        add("durability_posture", posture, category="durability", source="derived")
        add("durability_note", note, category="durability", source="derived")

        for key, value in _server(conn).items():
            add(key, value, category="server")

        for key, value in _role(conn).items():
            add(key, value, category="roles")

        for key, value in _extensions(conn).items():
            add(key, value, category="extensions")

        for key, value in _pooling_probe(conn).items():
            add(key, value, category="pooling", source="probe")

        if probe_gucs:
            for key, value in _guc_probe(conn).items():
                add(key, value, category="gucs", source="probe")

    return facts


def _settings(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "select name, setting, unit from pg_settings where name = any(%s) order by name",
        (list(_SETTINGS),),
    ).fetchall()
    return {name: f"{setting}{unit or ''}" for name, setting, unit in rows}


def _server(conn: psycopg.Connection) -> dict[str, str]:
    row = conn.execute(
        """
        select current_setting('server_version'),
               current_setting('server_version_num'),
               pg_encoding_to_char(d.encoding), d.datcollate, current_database()
        from pg_database d where d.datname = current_database()
        """
    ).fetchone()
    if row is None:
        return {}
    return {
        "server_version": row[0],
        "server_version_num": row[1],
        "encoding": row[2],
        "collate": row[3],
        "database": row[4],
    }


def _role(conn: psycopg.Connection) -> dict[str, str]:
    row = conn.execute(
        """
        select rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit
        from pg_roles where rolname = current_user
        """
    ).fetchone()
    if row is None:
        return {}
    return {
        "role_superuser": str(row[0]).lower(),
        "role_createdb": str(row[1]).lower(),
        "role_createrole": str(row[2]).lower(),
        "role_replication": str(row[3]).lower(),
        "role_bypassrls": str(row[4]).lower(),
        "role_connection_limit": str(row[5]),
    }


def _extensions(conn: psycopg.Connection) -> dict[str, str]:
    available = {
        name: version
        for name, version in conn.execute(
            "select name, default_version from pg_available_extensions"
        ).fetchall()
    }
    installed = {
        name: version
        for name, version in conn.execute("select extname, extversion from pg_extension").fetchall()
    }
    out: dict[str, str] = {
        "extensions_available_count": str(len(available)),
        "extensions_installed": ", ".join(f"{n} {v}" for n, v in sorted(installed.items())) or "-",
    }
    for name in _NOTABLE_EXTENSIONS:
        if name in installed:
            out[f"ext_{name}"] = f"installed {installed[name]}"
        elif name in available:
            out[f"ext_{name}"] = f"available {available[name]}"
        else:
            out[f"ext_{name}"] = "absent"
    return out


def _pooling_probe(conn: psycopg.Connection) -> dict[str, str]:
    """Detect a pooler by behavior rather than by hostname or port convention.

    Under transaction pooling consecutive statements can land on different server
    backends, and a prepared statement does not survive to the next transaction. Both are
    things an application will hit; neither is visible in ``pg_settings``.
    """
    pids = {
        conn.execute("select pg_backend_pid()").fetchone()[0]  # type: ignore[index]
        for _ in range(5)
    }
    out = {
        "backend_pid_stable": str(len(pids) == 1).lower(),
        "distinct_backends_in_5_queries": str(len(pids)),
    }

    try:
        conn.execute("deallocate all")
        conn.execute("prepare capybench_probe as select 1")
        conn.execute("execute capybench_probe").fetchone()
        out["prepared_statements_persist"] = "true"
        conn.execute("deallocate capybench_probe")
    except psycopg.Error as exc:
        out["prepared_statements_persist"] = "false"
        out["prepared_statements_error"] = str(exc).strip()[:200]
    return out


def _guc_probe(conn: psycopg.Connection) -> dict[str, str]:
    """Which GUCs this role may actually set, proven by setting and rolling back.

    ``set_config(name, current_setting(name), true)`` is a no-op value-wise, so the only
    thing being tested is permission. Each probe runs in its own transaction: a rejected
    SET aborts the surrounding transaction, so sharing one would poison later probes.
    """
    out: dict[str, str] = {}
    settable: list[str] = []
    for guc in _PROBE_GUCS:
        try:
            with conn.transaction(force_rollback=True):
                conn.execute("select set_config(%s, current_setting(%s), true)", (guc, guc))
            settable.append(guc)
        except psycopg.Error:
            pass
    out["gucs_settable"] = ", ".join(settable) or "none"
    out["gucs_rejected"] = ", ".join(g for g in _PROBE_GUCS if g not in settable) or "none"
    return out


def durability_posture(settings: dict[str, str]) -> tuple[str, str]:
    """Classify how much durability the measured numbers were bought with.

    Kept a pure function of the settings dict so the classification is unit-testable and
    identical across every platform - this is the verdict that decides whether two
    throughput numbers may sit on the same chart.
    """
    fsync = settings.get("fsync", "unknown")
    sync_commit = settings.get("synchronous_commit", "unknown")
    fpw = settings.get("full_page_writes", "unknown")

    if fsync == "off":
        return "unsafe", "fsync=off: a crash can lose or corrupt the whole database"
    if sync_commit == "off":
        return (
            "relaxed",
            "synchronous_commit=off: commits return before WAL is durable, so a crash "
            "loses recent transactions. Write throughput is not comparable with a "
            "durable configuration.",
        )
    if sync_commit in ("local", "remote_write"):
        return (
            "relaxed",
            f"synchronous_commit={sync_commit}: weaker than 'on' for replicated setups.",
        )
    if fpw == "off":
        return (
            "relaxed",
            "full_page_writes=off: safe only on storage with atomic writes; verify the "
            "platform actually provides that before comparing.",
        )
    if fsync == "unknown" or sync_commit == "unknown":
        return "unknown", "durability settings were not readable on this target"
    return "full", f"fsync={fsync}, synchronous_commit={sync_commit}, full_page_writes={fpw}"
