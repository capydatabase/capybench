"""``pgbench`` wrapper.

pgbench is the classic, universally-recognized Postgres benchmark. It reports TPS and
average latency but *not* tail percentiles — for p95/p99 use the sysbench runner. This
wrapper is used both for the throughput baseline and as the aggressor in the
noisy-neighbor scenario.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from ..config import Target

_TPS_RE = re.compile(r"^tps = ([\d.]+)", re.MULTILINE)
_LAT_AVG_RE = re.compile(r"latency average = ([\d.]+) ms")
_LAT_STDDEV_RE = re.compile(r"latency stddev = ([\d.]+) ms")
_TXNS_RE = re.compile(r"number of transactions actually processed: (\d+)")


@dataclass(slots=True)
class PgbenchResult:
    tps: float
    latency_avg_ms: float
    latency_stddev_ms: float | None
    transactions: int
    stdout: str


def _base_flags(target: Target) -> list[str]:
    return [
        "-h", target.host,
        "-p", str(target.port),
        "-U", target.user,
        "-d", target.dbname,
    ]


def _env(target: Target) -> dict[str, str]:
    import os

    env = os.environ.copy()
    if target.password is not None:
        env["PGPASSWORD"] = target.password
    env["PGSSLMODE"] = target.sslmode
    return env


def initialize(target: Target, *, scale: int, bin_path: str = "pgbench") -> None:
    """Populate the pgbench tables (``pgbench -i -s <scale>``)."""
    proc = subprocess.run(
        [bin_path, "-i", "-s", str(scale), *_base_flags(target)],
        env=_env(target),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pgbench init failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
        )


def run(
    target: Target,
    *,
    clients: int,
    duration_s: int,
    threads: int | None = None,
    read_only: bool = False,
    bin_path: str = "pgbench",
) -> PgbenchResult:
    """Run a timed pgbench and parse TPS/latency out of its report."""
    cmd = [
        bin_path,
        "-c", str(clients),
        "-j", str(threads or min(clients, 8)),
        "-T", str(duration_s),
        "--progress", "10",
        *_base_flags(target),
    ]
    if read_only:
        cmd += ["-S"]  # SELECT-only (read) transaction script

    proc = subprocess.run(
        cmd, env=_env(target), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pgbench run failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
        )

    out = proc.stdout
    tps = _search_float(_TPS_RE, out, "tps", target)
    lat_avg = _search_float(_LAT_AVG_RE, out, "latency average", target)
    stddev_m = _LAT_STDDEV_RE.search(out)
    txns_m = _TXNS_RE.search(out)

    return PgbenchResult(
        tps=tps,
        latency_avg_ms=lat_avg,
        latency_stddev_ms=float(stddev_m.group(1)) if stddev_m else None,
        transactions=int(txns_m.group(1)) if txns_m else 0,
        stdout=out,
    )


def spawn(
    target: Target,
    *,
    clients: int,
    duration_s: int,
    read_only: bool = False,
    bin_path: str = "pgbench",
) -> subprocess.Popen[str]:
    """Start pgbench without waiting — used to run an aggressor concurrently."""
    cmd = [
        bin_path,
        "-c", str(clients),
        "-j", str(min(clients, 8)),
        "-T", str(duration_s),
        *_base_flags(target),
    ]
    if read_only:
        cmd += ["-S"]
    return subprocess.Popen(
        cmd, env=_env(target), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _search_float(pattern: re.Pattern[str], text: str, label: str, target: Target) -> float:
    match = pattern.search(text)
    if match is None:
        raise RuntimeError(
            f"could not parse {label!r} from pgbench output on {target.name}:\n{text}"
        )
    return float(match.group(1))
