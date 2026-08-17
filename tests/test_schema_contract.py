"""The results schema and the code that writes to it must not drift apart.

``store.py`` builds INSERT statements by hand, so a column added to one side and not the
other fails only at runtime - after a benchmark has already spent its time. These tests
compare the two offline:

* every column ``store.py`` inserts into exists in ``sql/001_results_schema.sql``
* every column ``001`` defines as NOT NULL without a default is actually supplied
* the migration in ``003`` adds exactly what ``001`` already contains, so a fresh install
  and an upgraded one end up with the same schema
"""

from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
STORE_PY = Path(__file__).resolve().parent.parent / "src" / "capybench" / "store.py"


def _table_columns(sql: str, table: str) -> dict[str, str]:
    """``{column: definition}`` from a ``create table`` block."""
    match = re.search(
        rf"create table if not exists {table} \((.*?)\n\);", sql, re.DOTALL | re.IGNORECASE
    )
    assert match, f"{table} not found in schema"

    columns: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.split("--")[0].strip().rstrip(",")
        if not stripped or stripped.lower().startswith(("primary key", "unique", "foreign")):
            continue
        name, _, definition = stripped.partition(" ")
        columns[name] = definition.strip()
    return columns


def _insert_columns(source: str, table: str) -> set[str]:
    """Column names from the ``insert into <table> (...)`` statement in store.py."""
    match = re.search(rf"insert into {table} \((.*?)\)\s*\n\s*values", source, re.DOTALL)
    assert match, f"no insert into {table} found in store.py"
    return {col.strip() for col in match.group(1).replace("\n", " ").split(",") if col.strip()}


def _placeholder_count(source: str, table: str) -> int:
    match = re.search(rf"insert into {table} \(.*?\)\s*\n\s*values \((.*?)\)", source, re.DOTALL)
    assert match, f"no values clause for {table}"
    return match.group(1).count("%s")


SCHEMA = (SQL_DIR / "001_results_schema.sql").read_text(encoding="utf-8")
MIGRATION = (SQL_DIR / "003_facts_and_vantage.sql").read_text(encoding="utf-8")
STORE = STORE_PY.read_text(encoding="utf-8")


def test_sample_insert_matches_schema() -> None:
    columns = _table_columns(SCHEMA, "bench_sample")
    inserted = _insert_columns(STORE, "bench_sample")
    assert inserted <= set(columns), f"unknown columns: {inserted - set(columns)}"
    assert "vantage" in inserted, "samples must record which vantage they were taken from"


def test_fact_insert_matches_schema() -> None:
    columns = _table_columns(SCHEMA, "bench_fact")
    inserted = _insert_columns(STORE, "bench_fact")
    assert inserted <= set(columns), f"unknown columns: {inserted - set(columns)}"


def test_run_insert_matches_schema() -> None:
    columns = _table_columns(SCHEMA, "bench_run")
    inserted = _insert_columns(STORE, "bench_run")
    assert inserted <= set(columns), f"unknown columns: {inserted - set(columns)}"
    assert "attestation" in inserted


def test_every_insert_binds_one_placeholder_per_column() -> None:
    for table in ("bench_run", "bench_sample", "bench_fact"):
        assert len(_insert_columns(STORE, table)) == _placeholder_count(STORE, table), (
            f"{table}: column count and %s placeholder count disagree"
        )


def test_required_columns_are_always_supplied() -> None:
    """A NOT NULL column without a default must appear in the insert."""
    for table in ("bench_sample", "bench_fact"):
        columns = _table_columns(SCHEMA, table)
        inserted = _insert_columns(STORE, table)
        for name, definition in columns.items():
            lowered = definition.lower()
            if "not null" in lowered and "default" not in lowered and "generated" not in lowered:
                if name.endswith("_id") and "references" in lowered:
                    assert name in inserted, f"{table}.{name} is required but never inserted"
                    continue
                assert name in inserted, f"{table}.{name} is required but never inserted"


def test_migration_and_fresh_install_agree() -> None:
    """Anything 003 adds must already be in 001, or upgraded DBs diverge from fresh ones."""
    assert "add column if not exists attestation" in MIGRATION
    assert "add column if not exists vantage" in MIGRATION
    assert "attestation" in _table_columns(SCHEMA, "bench_run")
    assert "vantage" in _table_columns(SCHEMA, "bench_sample")

    migration_fact_columns = set(_table_columns(MIGRATION, "bench_fact"))
    schema_fact_columns = set(_table_columns(SCHEMA, "bench_fact"))
    assert migration_fact_columns == schema_fact_columns


def test_vantage_defaults_to_client_everywhere() -> None:
    """Rows written by an older harness must read back as client-vantage, not NULL."""
    assert "'client'" in _table_columns(SCHEMA, "bench_sample")["vantage"]
    assert "'client'" in _table_columns(SCHEMA, "bench_fact")["vantage"]
    assert "default 'client'" in MIGRATION
