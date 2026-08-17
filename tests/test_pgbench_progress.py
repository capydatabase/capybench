"""Unit tests for pgbench --progress parsing.

Progress lines carry two loads: verifying an aggressor actually ran (noisy_neighbor) and
reconstructing a long run's shape over time (sustained). Both depend on this parse, so a
format change on a new pgbench must fail here rather than silently yield an empty series.
"""

from capybench.runners.pgbench import parse_progress, parse_progress_tps

STDERR = """\
pgbench (16.14)
progress: 5.0 s, 2412.3 tps, lat 19.868 ms stddev 11.020, 0 failed
progress: 10.0 s, 2601.0 tps, lat 18.412 ms stddev 10.115, 0 failed
progress: 15.0 s, 0.0 tps, lat 0.000 ms stddev 0.000, 0 failed
transaction type: <builtin: TPC-B (sort of)>
"""

# Older builds omit the latency clause entirely.
STDERR_NO_LATENCY = """\
progress: 60.0 s, 812.5 tps
progress: 120.0 s, 799.1 tps
"""


def test_parses_progress_tps_lines() -> None:
    assert parse_progress_tps(STDERR) == [2412.3, 2601.0, 0.0]


def test_no_progress_lines_returns_empty() -> None:
    assert parse_progress_tps("pgbench (16.14)\nfatal: connection failed\n") == []


def test_parses_windows_with_elapsed_and_latency() -> None:
    windows = parse_progress(STDERR)
    assert [w.elapsed_s for w in windows] == [5.0, 10.0, 15.0]
    assert windows[0].latency_avg_ms == 19.868
    assert windows[2].tps == 0.0


def test_windows_parse_without_a_latency_clause() -> None:
    windows = parse_progress(STDERR_NO_LATENCY)
    assert [w.tps for w in windows] == [812.5, 799.1]
    assert all(w.latency_avg_ms is None for w in windows)


def test_elapsed_seconds_are_monotonic() -> None:
    """The sustained chart plots against elapsed time, so ordering must survive parsing."""
    elapsed = [w.elapsed_s for w in parse_progress(STDERR)]
    assert elapsed == sorted(elapsed)
