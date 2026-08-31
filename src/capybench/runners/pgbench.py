"""``pgbench`` wrapper.

pgbench is the classic, universally-recognized Postgres benchmark. Its summary report
has TPS and average latency only, so tail percentiles are recovered from per-transaction
logs (``-l``): with ``collect_percentiles=True`` the wrapper parses the latency column of
every log line and computes exact p50/p95/p99 - no histogram approximation. This wrapper
is used both for the throughput baseline and as the aggressor in the noisy-neighbor
scenario.

CLOSED VS OPEN LOOP. By default pgbench is a *closed-loop* generator: each client waits
for its previous transaction to finish before issuing the next. That is the right shape
for comparing behaviour at a fixed concurrency, but it systematically flatters tail
latency - when the database stalls, the client stalls with it, so the queue of work that
*would* have arrived is never issued and never measured. This is coordinated omission,
and it is why a closed-loop p99 can look fine through an incident that users experienced
as an outage.

Passing ``rate_tps`` switches to an *open loop*: pgbench schedules transactions at a
fixed arrival rate regardless of how fast the database is answering, which is how
production load actually behaves. It then reports how far behind schedule each
transaction started ("schedule lag"), and
:attr:`PgbenchResult.latency_including_lag_p99_ms` adds that lag back in - the number a
user would have experienced, as opposed to the number the server saw once it finally got
the request.
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
_LAG_AVG_RE = re.compile(r"rate limit schedule lag: avg ([\d.]+) ")
_SKIPPED_RE = re.compile(r"number of transactions skipped: (\d+)")


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
    # Open-loop only (rate_tps set). Percentiles of schedule lag + service time: what a
    # caller waiting since the transaction was DUE actually experienced. These are the
    # coordinated-omission-free numbers; the plain latency_p* fields above stay
    # comparable with closed-loop runs.
    latency_including_lag_p50_ms: float | None = None
    latency_including_lag_p95_ms: float | None = None
    latency_including_lag_p99_ms: float | None = None
    # Mean schedule lag reported by pgbench; a value near zero means the requested rate
    # was comfortably served, and a growing one means the database could not keep up.
    schedule_lag_avg_ms: float | None = None
    # Transactions pgbench gave up on because they fell further behind schedule than
    # latency_limit_ms allowed. Non-zero means the target rate was not achievable.
    transactions_skipped: int = 0
    # The requested arrival rate, echoed back so a stored result is self-describing.
    rate_tps: float | None = None


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
    rate_tps: float | None = None,
    latency_limit_ms: float | None = None,
    bin_path: str = "pgbench",
) -> PgbenchResult:
    """Run a timed pgbench and parse TPS/latency out of its report.

    With ``collect_percentiles=True`` pgbench also writes per-transaction logs (``-l``)
    into a temporary directory; the latency column is parsed into exact p50/p95/p99.

    ``progress_s`` sets the ``--progress`` interval; the parsed windows come back on
    :attr:`PgbenchResult.progress`, which is how a long run's *shape* (burst-credit
    exhaustion, checkpoint stalls) becomes visible instead of averaging away.

    ``rate_tps`` switches the run to an OPEN loop (``--rate``): transactions are issued
    on a fixed schedule instead of one-per-client-as-fast-as-possible, so a stall shows
    up as a queue rather than silently reducing the offered load. See the module
    docstring on coordinated omission. It implies ``collect_percentiles`` - the
    lag-inclusive percentiles are the entire reason to run open-loop, and they come from
    the per-transaction log.

    ``latency_limit_ms`` (open loop only) makes pgbench abandon transactions that have
    already fallen further behind schedule than the limit, which is what a real client
    with a timeout would do. Abandoned transactions are counted in
    :attr:`PgbenchResult.transactions_skipped` rather than distorting the percentiles.
    """
    if rate_tps is not None:
        collect_percentiles = True
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
    if rate_tps is not None:
        cmd += ["--rate", str(rate_tps)]
        if latency_limit_ms is not None:
            cmd += ["--latency-limit", str(latency_limit_ms)]

    with tempfile.TemporaryDirectory(prefix="capybench-pgbench-") as tmp:
        if collect_percentiles:
            cmd += ["-l", "--log-prefix", f"{tmp}/txn"]

        proc = subprocess.run(cmd, env=_env(target), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pgbench run failed on {target.name} (rc={proc.returncode}):\n{proc.stderr}"
            )

        p50 = p95 = p99 = None
        lag_p50 = lag_p95 = lag_p99 = None
        if collect_percentiles:
            latencies_ms, lags_ms = parse_transaction_logs(Path(tmp))
            if not latencies_ms:
                raise RuntimeError(
                    f"pgbench wrote no per-transaction log lines on {target.name}; "
                    "cannot compute percentiles"
                )
            p50 = percentile(latencies_ms, 50)
            p95 = percentile(latencies_ms, 95)
            p99 = percentile(latencies_ms, 99)
            if lags_ms:
                # Service time plus how long the transaction sat waiting for a client
                # that was busy. Summing per-transaction (rather than adding two
                # percentiles) keeps the pairing intact - the slowest transactions are
                # usually also the most delayed, and adding percentiles would
                # understate exactly the tail this measurement exists to expose.
                total_ms = [lat + lag for lat, lag in zip(latencies_ms, lags_ms, strict=True)]
                lag_p50 = percentile(total_ms, 50)
                lag_p95 = percentile(total_ms, 95)
                lag_p99 = percentile(total_ms, 99)

    out = proc.stdout
    tps = _search_float(_TPS_RE, out, "tps", target)
    lat_avg = _search_float(_LAT_AVG_RE, out, "latency average", target)
    stddev_m = _LAT_STDDEV_RE.search(out)
    txns_m = _TXNS_RE.search(out)
    lag_m = _LAG_AVG_RE.search(out)
    skipped_m = _SKIPPED_RE.search(out)

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
        latency_including_lag_p50_ms=lag_p50,
        latency_including_lag_p95_ms=lag_p95,
        latency_including_lag_p99_ms=lag_p99,
        schedule_lag_avg_ms=float(lag_m.group(1)) if lag_m else None,
        transactions_skipped=int(skipped_m.group(1)) if skipped_m else 0,
        rate_tps=rate_tps,
    )


def parse_transaction_logs(
    log_dir: Path, *, prefix: str = "txn"
) -> tuple[list[float], list[float]]:
    """Parse pgbench ``-l`` log files into per-transaction latencies and schedule lags.

    Log line format: ``client_id transaction_no time script_no time_epoch time_us``
    where ``time`` (third column) is the transaction latency in microseconds. Under
    ``--rate`` pgbench appends a seventh column, ``schedule_lag``, also in microseconds:
    how long after its scheduled start the transaction actually began.

    Returns ``(latencies_ms, lags_ms)``. The lag list is empty for a closed-loop run,
    and is otherwise the same length as the latency list, index for index.
    """
    latencies_ms: list[float] = []
    lags_ms: list[float] = []
    for log_file in sorted(log_dir.glob(f"{prefix}.*")):
        for line in log_file.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 3:
                continue
            try:
                latency = float(fields[2]) / 1000.0
            except ValueError:
                continue  # interval-start markers etc.
            lag: float | None = None
            if len(fields) >= 7:
                try:
                    lag = float(fields[6]) / 1000.0
                except ValueError:
                    lag = None
            # Keep the two lists index-aligned: a line whose lag column is missing or
            # unparseable would otherwise silently shift every later pairing.
            if lag is None and lags_ms:
                continue
            if lag is not None and not lags_ms and latencies_ms:
                continue
            latencies_ms.append(latency)
            if lag is not None:
                lags_ms.append(lag)
    return latencies_ms, lags_ms


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
