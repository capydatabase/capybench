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

# Under --rate pgbench appends a seventh column: schedule_lag, in microseconds.
LOG_RATE = """\
0 1 2100 0 1751900000 123456 500
0 2 1500 0 1751900000 234567 12000
1 1 3200 0 1751900000 345678 0
"""


def test_parses_latency_column_across_files(tmp_path: Path) -> None:
    (tmp_path / "txn.101").write_text(LOG_A, encoding="utf-8")
    (tmp_path / "txn.101.1").write_text(LOG_B, encoding="utf-8")
    latencies, lags = parse_transaction_logs(tmp_path)
    assert sorted(latencies) == [0.9, 1.5, 2.1, 3.2]  # µs -> ms
    assert lags == []  # closed loop: no schedule column


def test_ignores_files_without_prefix(tmp_path: Path) -> None:
    (tmp_path / "other.101").write_text(LOG_A, encoding="utf-8")
    assert parse_transaction_logs(tmp_path) == ([], [])


def test_skips_malformed_lines(tmp_path: Path) -> None:
    (tmp_path / "txn.7").write_text("garbage\n0 1\n0 1 notanumber 0 1 1\n" + LOG_B, "utf-8")
    assert parse_transaction_logs(tmp_path) == ([0.9], [])


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert parse_transaction_logs(tmp_path) == ([], [])


def test_parses_schedule_lag_from_open_loop_logs(tmp_path: Path) -> None:
    (tmp_path / "txn.9").write_text(LOG_RATE, encoding="utf-8")
    latencies, lags = parse_transaction_logs(tmp_path)
    assert latencies == [2.1, 1.5, 3.2]
    assert lags == [0.5, 12.0, 0.0]


def test_lag_and_latency_stay_index_aligned(tmp_path: Path) -> None:
    """A lagless line inside an open-loop log must not shift the pairing.

    The lag-inclusive percentiles sum the two lists position by position, so a single
    dropped lag would silently attribute every later transaction's delay to the wrong
    transaction - and the tail is exactly what this measurement exists to expose.
    """
    (tmp_path / "txn.9").write_text(
        "0 1 2100 0 1751900000 123456 500\n0 2 1500 0 1751900000 234567\n", encoding="utf-8"
    )
    latencies, lags = parse_transaction_logs(tmp_path)
    assert len(latencies) == len(lags)
    assert latencies == [2.1]
    assert lags == [0.5]
