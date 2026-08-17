"""``pgbench`` wrapper.

pgbench is the classic, universally-recognized Postgres benchmark. Its summary report
has TPS and average latency only, so tail percentiles are recovered from per-transaction
logs (``-l``): with ``collect_percentiles=True`` the wrapper parses the latency column of
every log line and computes exact p50/p95/p99 - no histogram approximation. This wrapper
is used both for the throughput baseline and as the aggressor in the noisy-neighbor
scenario.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Target
from ..stats import percentile

_TPS_RE = re.compile(r"^tps = ([\d.]+)", re.MULTILINE)
_LAT_AVG_RE = re.compile(r"latency average = ([\d.]+) ms")
_LAT_STDDEV_RE = re.compile(r"latency stddev = ([\d.]+) ms")
_TXNS_RE = re.compile(r"number of transactions actually processed: (\d+)")


@dataclass(frozen=True, slots=True)
class ProgressWindow:
    """One ``--progress`` interval report: the run's shape over time, not just its total."""

    elapsed_s: float
    tps: float
    latency_avg_ms: float | None


@dataclass(slots=True)
class PgbenchResult:
    tps: float
    latency_avg_ms: float
    latency_stddev_ms: float | None
    transactions: int
    stdout: str
    # Exact percentiles from per-transaction logs; None unless collect_percentiles=True.
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    # Per-interval reports parsed from stderr; empty if pgbench printed none.
    progress: tuple[ProgressWindow, ...] = ()


def _base_flags(target: Target) -> list[str]:
    # NB: no ``-d`` here - on pgbench <= 16 ``-d`` means --debug (dbname is positional),
    # which floods stderr with per-client lines and can stall a spawned aggressor once
    # the pipe buffer fills. The dbname travels via PGDATABASE (see _env) instead.
    return [
        "-h",
        target.host,
        "-p",
        str(target.port),
        "-U",
        target.user,
    ]


def _env(target: Target) -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["PGDATABASE"] = target.dbname
    if target.password is not None:
        env["PGPASSWORD"] = target.password
    env["PGSSLMODE"] = target.sslmode
    if target.sslrootcert is not None:
        env["PGSSLROOTCERT"] = target.sslrootcert
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
    collect_percentiles: bool = False,
    progress_s: int = 10,
    bin_path: str = "pgbench",
) -> PgbenchResult:
    """Run a timed pgbench and parse TPS/latency out of its report.

    With ``collect_percentiles=True`` pgbench also writes per-transaction logs (``-l``)
    into a temporary directory; the latency column is parsed into exact p50/p95/p99.

    ``progress_s`` sets the ``--progress`` interval; the parsed windows come back on
    :attr:`PgbenchResult.progress`, which is how a long run's *shape* (burst-credit
    exhaustion, checkpoint stalls) becomes visible instead of averaging away.
    """
    cmd = [
        bin_path,
        "-c",
        str(clients),
        "-j",
        str(threads or min(clients, 8)),
        "-T",
        str(duration_s),
        "--progress",
        str(progress_s),
        *_base_flags(target),
    ]
    if read_only:
        cmd += ["-S"]  # SELECT-only (read) transaction script

    with tempfile.TemporaryDirectory(prefix="capybench-pgbench-") as tmp:
        if collect_percentiles:
            cmd += ["-l", "--log-prefix", f"{tmp}/txn"]

        proc = subprocess.run(cmd, env=_env(target), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pgbench run failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
            )

        p50 = p95 = p99 = None
        if collect_percentiles:
            latencies_ms = parse_transaction_logs(Path(tmp))
            if not latencies_ms:
                raise RuntimeError(
                    f"pgbench wrote no per-transaction log lines on {target.name}; "
                    "cannot compute percentiles"
                )
            p50 = percentile(latencies_ms, 50)
            p95 = percentile(latencies_ms, 95)
            p99 = percentile(latencies_ms, 99)

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
        latency_p50_ms=p50,
        latency_p95_ms=p95,
        latency_p99_ms=p99,
        progress=tuple(parse_progress(proc.stderr)),
    )


def parse_transaction_logs(log_dir: Path, *, prefix: str = "txn") -> list[float]:
    """Parse pgbench ``-l`` log files into per-transaction latencies in ms.

    Log line format: ``client_id transaction_no time script_no time_epoch time_us``
    where ``time`` (third column) is the transaction latency in microseconds.
    """
    latencies_ms: list[float] = []
    for log_file in sorted(log_dir.glob(f"{prefix}.*")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                latencies_ms.append(float(fields[2]) / 1000.0)
            except ValueError:
                continue  # interval-start markers etc.
    return latencies_ms


def spawn(
    target: Target,
    *,
    clients: int,
    duration_s: int,
    read_only: bool = False,
    bin_path: str = "pgbench",
) -> subprocess.Popen[str]:
    """Start pgbench without waiting - used to run an aggressor concurrently.

    ``--progress 5`` interval reports go to stderr; parse them after the process ends
    with :func:`parse_progress_tps` to prove the load was actually sustained.
    """
    cmd = [
        bin_path,
        "-c",
        str(clients),
        "-j",
        str(min(clients, 8)),
        "-T",
        str(duration_s),
        "--progress",
        "5",
        *_base_flags(target),
    ]
    if read_only:
        cmd += ["-S"]
    return subprocess.Popen(
        cmd, env=_env(target), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


# "progress: 60.0 s, 1234.5 tps, lat 8.123 ms stddev 2.345" - the latency clause is
# absent on some builds/versions, so it stays optional.
_PROGRESS_RE = re.compile(
    r"^progress: ([\d.]+) s, ([\d.]+) tps(?:, lat ([\d.]+) ms)?", re.MULTILINE
)


def parse_progress(stderr_text: str) -> list[ProgressWindow]:
    """Parse pgbench ``--progress`` interval lines (stderr) into windows."""
    return [
        ProgressWindow(
            elapsed_s=float(elapsed),
            tps=float(tps),
            latency_avg_ms=float(lat) if lat else None,
        )
        for elapsed, tps, lat in _PROGRESS_RE.findall(stderr_text)
    ]


def parse_progress_tps(stderr_text: str) -> list[float]:
    """TPS values from pgbench ``--progress`` interval lines (stderr)."""
    return [window.tps for window in parse_progress(stderr_text)]


def _search_float(pattern: re.Pattern[str], text: str, label: str, target: Target) -> float:
    match = pattern.search(text)
    if match is None:
        raise RuntimeError(
            f"could not parse {label!r} from pgbench output on {target.name}:\n{text}"
        )
    return float(match.group(1))
