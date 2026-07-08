"""``sysbench`` wrapper (oltp_read_write / oltp_read_only).

sysbench gives a more realistic OLTP mix than pgbench *and* reports tail latency
percentiles, which is what the isolation story lives or dies on. Install: ``brew install
sysbench`` (macOS) / ``apt-get install sysbench`` (Debian). The pgsql driver must be
present in the build.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from ..config import Target

_TPS_RE = re.compile(r"transactions:\s+\d+\s+\(([\d.]+)\s+per sec")
_LAT_AVG_RE = re.compile(r"^\s*avg:\s+([\d.]+)", re.MULTILINE)
_LAT_PCT_RE = re.compile(r"(\d+)(?:th|st|nd|rd) percentile:\s+([\d.]+)")


@dataclass(slots=True)
class SysbenchResult:
    tps: float
    latency_avg_ms: float
    percentile: int
    latency_pct_ms: float
    stdout: str


def _common_flags(target: Target, *, threads: int, duration_s: int, percentile: int) -> list[str]:
    flags = [
        "--db-driver=pgsql",
        f"--pgsql-host={target.host}",
        f"--pgsql-port={target.port}",
        f"--pgsql-user={target.user}",
        f"--pgsql-db={target.dbname}",
        f"--threads={threads}",
        f"--time={duration_s}",
        f"--percentile={percentile}",
    ]
    if target.password is not None:
        flags.append(f"--pgsql-password={target.password}")
    return flags


def _script(read_only: bool) -> str:
    return "oltp_read_only" if read_only else "oltp_read_write"


def prepare(
    target: Target, *, tables: int = 8, table_size: int = 100_000, bin_path: str = "sysbench"
) -> None:
    """Create and fill sysbench tables. ``table_size`` rows per table."""
    cmd = [
        bin_path,
        _script(read_only=False),
        *_common_flags(target, threads=1, duration_s=1, percentile=99),
        f"--tables={tables}",
        f"--table-size={table_size}",
        "prepare",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"sysbench prepare failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
        )


def run(
    target: Target,
    *,
    threads: int,
    duration_s: int,
    percentile: int = 99,
    read_only: bool = False,
    tables: int = 8,
    table_size: int = 100_000,
    bin_path: str = "sysbench",
) -> SysbenchResult:
    """Run a timed sysbench oltp workload and parse TPS + tail latency."""
    cmd = [
        bin_path,
        _script(read_only),
        *_common_flags(target, threads=threads, duration_s=duration_s, percentile=percentile),
        f"--tables={tables}",
        f"--table-size={table_size}",
        "run",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"sysbench run failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
        )

    out = proc.stdout
    tps_m = _TPS_RE.search(out)
    avg_m = _LAT_AVG_RE.search(out)
    pct_m = _LAT_PCT_RE.search(out)
    if tps_m is None or avg_m is None or pct_m is None:
        raise RuntimeError(f"could not parse sysbench output on {target.name}:\n{out}")

    return SysbenchResult(
        tps=float(tps_m.group(1)),
        latency_avg_ms=float(avg_m.group(1)),
        percentile=int(pct_m.group(1)),
        latency_pct_ms=float(pct_m.group(2)),
        stdout=out,
    )


def cleanup(
    target: Target, *, tables: int = 8, bin_path: str = "sysbench"
) -> None:
    """Drop sysbench tables."""
    cmd = [
        bin_path,
        _script(read_only=False),
        *_common_flags(target, threads=1, duration_s=1, percentile=99),
        f"--tables={tables}",
        "cleanup",
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=False)
