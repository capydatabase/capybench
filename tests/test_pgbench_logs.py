"""Unit tests for pgbench per-transaction log parsing (capybench.runners.pgbench)."""

from pathlib import Path

from capybench.runners.pgbench import parse_transaction_logs

# Real pgbench -l format: client_id transaction_no time script_no time_epoch time_us
LOG_A = """\
0 1 2100 0 1751900000 123456
0 2 1500 0 1751900000 234567
1 1 3200 0 1751900000 345678
"""

LOG_B = """\
2 1 900 0 1751900001 456789
"""


def test_parses_latency_column_across_files(tmp_path: Path) -> None:
    (tmp_path / "txn.101").write_text(LOG_A, encoding="utf-8")
    (tmp_path / "txn.101.1").write_text(LOG_B, encoding="utf-8")
    latencies = parse_transaction_logs(tmp_path)
    assert sorted(latencies) == [0.9, 1.5, 2.1, 3.2]  # µs -> ms


def test_ignores_files_without_prefix(tmp_path: Path) -> None:
    (tmp_path / "other.101").write_text(LOG_A, encoding="utf-8")
    assert parse_transaction_logs(tmp_path) == []


def test_skips_malformed_lines(tmp_path: Path) -> None:
    (tmp_path / "txn.7").write_text("garbage\n0 1\n0 1 notanumber 0 1 1\n" + LOG_B, "utf-8")
    assert parse_transaction_logs(tmp_path) == [0.9]


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert parse_transaction_logs(tmp_path) == []
