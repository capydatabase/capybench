"""CapyBench command-line entrypoint.

capybench run    --config capybench.toml [--only throughput,cold_start] [--notes "..."]
capybench chart  --config capybench.toml [--run <uuid>] [--out charts/]
capybench report --config capybench.toml [--run <uuid>] [--out report.html]
capybench check  --config capybench.toml        # validate config + tools + providers
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any

import psycopg

from . import __version__, charts, config, hostprobe, providers, report, scenarios
from .capabilities import describe
from .config import Suite
from .hostprobe import HostProbe, HostProbeError
from .store import Fact, Run, Sample, Vantage


def _log(message: str) -> None:
    print(f"[capybench] {message}", file=sys.stderr, flush=True)


def _git_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _load_suite(path: str) -> Suite | None:
    """Load a config, turning every failure mode into a clear message (no traceback)."""
    try:
        return config.load(path)
    except FileNotFoundError:
        _log(f"config file not found: {path} (copy capybench.toml.example to get started)")
    except tomllib.TOMLDecodeError as exc:
        _log(f"config {path} is not valid TOML: {exc}")
    except (KeyError, TypeError, ValueError) as exc:
        _log(f"config {path} is invalid: {exc}")
    return None


def _tool_version(*argv: str) -> str | None:
    """First line of ``argv --version``, or None. Never raises: this is metadata."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or out.stderr or "").strip().splitlines()
    return line[0].strip() if line else None


def _client_info(suite: Suite) -> dict[str, Any]:
    """Reproducibility metadata stored on every run (bench_run.client).

    Results are only comparable between runs whose CLIENT is comparable. Fresh-connect
    in particular is dominated by TLS handshake + SCRAM on the client side, so a
    different image, libpq, or instance type moves it independently of the database.
    Capture enough to tell afterwards whether two runs are even comparable, and pin the
    client image in the config (see ``client_image`` in capybench.toml.example).
    """
    info: dict[str, Any] = {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "region": suite.client_region,
        "kernel": platform.release(),
        "libc": "-".join(platform.libc_ver()) or None,
        "pgbench": _tool_version("pgbench", "--version"),
        "sysbench": _tool_version("sysbench", "--version"),
        "psql": _tool_version("psql", "--version"),
        "client_image": suite.client_image,
    }
    try:
        import psycopg

        info["psycopg"] = psycopg.__version__
    except Exception:  # pragma: no cover - metadata only
        pass
    return info


# -- host vantage ---------------------------------------------------------------------


def _open_host_probe(suite: Suite) -> HostProbe | None:
    if suite.host_probe is None:
        return None
    try:
        return hostprobe.create(suite.host_probe)
    except (ValueError, HostProbeError) as exc:
        _log(f"host_probe disabled: {exc}")
        return None


def _record_host_facts(probe: HostProbe, run: Run) -> None:
    try:
        facts = probe.facts()
    except HostProbeError as exc:
        _log(f"host_probe: could not read node facts: {exc}")
        return
    for key, value in facts.items():
        run.record_fact(
            Fact(
                scenario="host_probe",
                target=probe.label,
                key=key,
                value=value,
                source="host",
                vantage=Vantage.HOST,
                category="host",
            )
        )
    _log(f"host_probe: {probe.label} - {len(facts)} node fact(s) recorded")


@contextmanager
def _host_window(probe: HostProbe, run: Run, scenario: str) -> Iterator[None]:
    """Snapshot the node around one scenario and record what changed.

    A probe failure must never take a scenario down with it - the client-vantage
    measurement is the primary result, and host readings are corroboration.
    """
    try:
        before = probe.sample()
    except HostProbeError as exc:
        _log(f"host_probe: skipping host readings for {scenario}: {exc}")
        yield
        return

    try:
        yield
    finally:
        try:
            after = probe.sample()
        except HostProbeError as exc:
            _log(f"host_probe: no closing sample for {scenario}: {exc}")
            return

        for key, reading in hostprobe.deltas(before, after).items():
            _record_host(run, probe, scenario, key, reading.value, reading.unit, "delta")
        for variant, snapshot in (("before", before), ("after", after)):
            for key, reading in snapshot.items():
                if not reading.counter:
                    _record_host(run, probe, scenario, key, reading.value, reading.unit, variant)


def _record_host(
    run: Run,
    probe: HostProbe,
    scenario: str,
    metric: str,
    value: float,
    unit: str,
    variant: str,
) -> None:
    run.record(
        Sample(
            scenario=scenario,
            target=probe.label,
            vantage=Vantage.HOST,
            variant=variant,
            metric=metric,
            value=value,
            unit=unit,
        )
    )


# -- commands -------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    suite = _load_suite(args.config)
    if suite is None:
        return 2
    only = set(args.only.split(",")) if args.only else set(scenarios.NAMES)
    unknown = only - set(scenarios.NAMES)
    if unknown:
        _log(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        _log(f"known scenarios: {', '.join(scenarios.NAMES)}")
        return 2

    if suite.client_region is None:
        _log(
            "warning: 'client_region' is not set in the config; results are only "
            "comparable from a client colocated with the targets - record where this "
            "client runs before publishing"
        )

    try:
        run = Run.start(
            suite.results_dsn,
            git_sha=_git_sha(),
            notes=args.notes or suite.notes,
            client=_client_info(suite),
            attestation=dict(suite.attestation),
        )
    except psycopg.Error as exc:
        _log(f"results DB not usable ({exc}); run `capybench check` and apply sql/*.sql")
        return 1

    probe = _open_host_probe(suite)
    with run:
        _log(f"run {run.run_id} started (scenarios: {', '.join(sorted(only))})")
        if probe is not None:
            _record_host_facts(probe, run)

        around = partial(_host_window, probe, run) if probe is not None else None
        repeats = args.repeats if args.repeats is not None else suite.run_repeats
        repeats = max(1, repeats)
        executed: list[str] = []
        for i in range(repeats):
            run.repeat = i
            if repeats > 1:
                _log(f"--- repeat {i + 1}/{repeats} ---")
            executed = scenarios.run_all(suite, run, only=only, log=_log, around=around)
        _log(
            f"run {run.run_id} complete ({len(executed)} scenario(s) executed"
            + (f" x {repeats} repeats)" if repeats > 1 else ")")
        )
        if repeats > 1:
            _log(f"summarise with: capybench summary --config <cfg> --run {run.run_id}")
    return 0


def _cmd_chart(args: argparse.Namespace) -> int:
    suite = _load_suite(args.config)
    if suite is None:
        return 2
    try:
        paths = charts.render_all(suite.results_dsn, args.out, run_id=args.run)
    except (RuntimeError, psycopg.Error) as exc:
        _log(str(exc))
        return 1
    if not paths:
        _log("no samples matched; nothing rendered")
        return 1
    for path in paths:
        _log(f"wrote {path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    suite = _load_suite(args.config)
    if suite is None:
        return 2
    try:
        path = report.render(suite, args.out, run_id=args.run)
    except (RuntimeError, psycopg.Error) as exc:
        _log(str(exc))
        return 1
    _log(f"wrote {path}")
    return 0


def _check_providers(suite: Suite) -> bool:
    """Validate provider wiring and print each target's capability set."""
    ok = True
    for target in suite.targets.values():
        try:
            provider = providers.for_target(suite, target)
        except (ValueError, providers.ProviderError) as exc:
            _log(f"  target {target.name}: {exc}")
            ok = False
            continue
        _log(
            f"  target {target.name}: provider {provider.name!r} (type={provider.type}), "
            f"capabilities: {describe(provider.capabilities)}"
        )
    return ok


def _check_scenarios(suite: Suite) -> None:
    """Preview exactly which scenarios will run, and why the others will not."""
    for scenario in scenarios.SPECS:
        if getattr(suite, scenario.config_attr) is None:
            _log(f"  {scenario.name}: not configured (no [{scenario.name}] block)")
            continue
        reason = scenarios.unsupported_reason(suite, scenario)
        if reason is None:
            _log(f"  {scenario.name}: will run")
        else:
            _log(f"  {scenario.name}: will SKIP - {reason}")


def _check_host_probe(suite: Suite) -> bool:
    if suite.host_probe is None:
        _log("  host_probe: not configured (client-vantage results only)")
        return True
    try:
        probe = hostprobe.create(suite.host_probe)
        facts = probe.facts()
        readings = probe.sample()
    except (ValueError, HostProbeError) as exc:
        _log(f"  host_probe: NOT usable ({exc})")
        return False
    _log(
        f"  host_probe: {probe.label} reachable - {facts.get('os', 'unknown OS')}, "
        f"kernel {facts.get('kernel', '?')}, {len(readings)} metric(s) per sample"
    )
    if not any(key.startswith("cgroup_") for key in readings):
        _log(
            "    note: no cgroup readings; set 'cgroup' in [host_probe] to capture "
            "per-tenant CPU throttling (the isolation ground truth)"
        )
    return True


def _cmd_summary(args: argparse.Namespace) -> int:
    """Aggregate a repeated run into a distribution.

    A single benchmark pass on a node that also serves live traffic is a point estimate
    of a noisy process - the same config re-measured minutes later moved by tens of
    percent. This prints median plus spread across repeats so a published number can be
    stated honestly, and flags any metric whose spread is wide enough that the median
    should not be quoted on its own.
    """
    suite = _load_suite(args.config)
    if suite is None:
        return 2
    try:
        conn = psycopg.connect(suite.results_dsn, autocommit=True)
    except psycopg.Error as exc:
        _log(f"results DB not usable ({exc})")
        return 1
    with conn:
        run_id = args.run
        if not run_id:
            row = conn.execute("select id::text from bench_run order by started_at desc limit 1").fetchone()
            if row is None:
                _log("no runs recorded")
                return 1
            run_id = row[0]
        rows = conn.execute(
            """
            select scenario, target, concurrency, variant, metric, unit,
                   count(*) n,
                   min(value) lo,
                   percentile_cont(0.5) within group (order by value) med,
                   max(value) hi
            from bench_sample
            where run_id = %s
            group by scenario, target, concurrency, variant, metric, unit
            having count(*) > 1
            order by scenario, target, concurrency nulls first, variant, metric
            """,
            (run_id,),
        ).fetchall()
    if not rows:
        _log(f"run {run_id} has no repeated metrics - re-run with --repeats N (N >= 5)")
        return 1
    print(f"run {run_id}: distribution across repeats")
    print(f"{'scenario':<16}{'target':<24}{'c':>4} {'metric':<22}{'n':>3} "
          f"{'median':>10}{'min':>10}{'max':>10}{'spread':>9}")
    noisy = 0
    for sc, tgt, conc, var, metric, unit, n, lo, med, hi in rows:
        spread = ((hi - lo) / med * 100.0) if med else 0.0
        if spread >= 15.0:
            noisy += 1
        label = metric if not var else f"{metric}[{var}]"
        print(f"{sc:<16}{(tgt or '-'):<24}{(conc if conc is not None else ''):>4} "
              f"{label:<22}{n:>3} {med:>10.2f}{lo:>10.2f}{hi:>10.2f}{spread:>8.0f}%")
    print()
    print(f"{noisy} metric(s) span >=15% of their median across repeats.")
    if noisy:
        print("Quote those as a range, not a point. Spread this wide usually means the "
              "node was shared with live workloads during the sweep.")
    return 0

def _cmd_check(args: argparse.Namespace) -> int:
    suite = _load_suite(args.config)
    if suite is None:
        return 2
    ok = True

    _log(f"config OK: {len(suite.targets)} target(s), results_dsn set")
    for tool in ("pgbench", "sysbench", "psql"):
        found = shutil.which(tool)
        _log(f"  {tool}: {'found at ' + found if found else 'MISSING'}")
        if tool != "psql" and not found:
            ok = False

    ok = _check_providers(suite) and ok
    _check_scenarios(suite)
    ok = _check_host_probe(suite) and ok

    if suite.attestation:
        _log(f"  attestation: {len(suite.attestation)} declared fact(s) (published as claimed)")

    try:
        with psycopg.connect(suite.results_dsn, connect_timeout=5) as conn:
            conn.execute("select 1 from bench_run limit 1")
            conn.execute("select 1 from bench_fact limit 1")
            conn.execute("select vantage from bench_sample limit 1")
        _log("  results DB: reachable, schema present")
    except psycopg.Error as exc:
        _log(
            f"  results DB: NOT ready ({exc}); apply sql/001_results_schema.sql, then any "
            f"later sql/NNN_*.sql migrations in order"
        )
        ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capybench", description=__doc__)
    parser.add_argument("--version", action="version", version=f"capybench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run benchmark scenarios")
    p_run.add_argument("--config", required=True)
    p_run.add_argument(
        "--only", help=f"comma-separated scenario subset ({', '.join(scenarios.NAMES)})"
    )
    p_run.add_argument("--notes", help="free-text note stored on the run")
    p_run.add_argument(
        "--repeats",
        type=int,
        default=None,
        help=(
            "execute the selected scenarios N times in one run, stamping params.repeat. "
            "A single pass on a shared node is not publishable - throughput here varies "
            "by tens of percent depending on what else is awake. Use >=5 and report the "
            "distribution via `capybench summary`."
        ),
    )
    p_run.set_defaults(func=_cmd_run)

    p_chart = sub.add_parser("chart", help="render SVG charts from stored samples")
    p_chart.add_argument("--config", required=True)
    p_chart.add_argument("--run", help="run UUID (default: latest)")
    p_chart.add_argument("--out", default="charts")
    p_chart.set_defaults(func=_cmd_chart)

    p_report = sub.add_parser(
        "report", help="render a single self-contained HTML report for one run"
    )
    p_report.add_argument("--config", required=True)
    p_report.add_argument("--run", help="run UUID (default: latest)")
    p_report.add_argument("--out", default="report.html")
    p_report.set_defaults(func=_cmd_report)

    p_summary = sub.add_parser(
        "summary", help="aggregate a repeated run into median + spread per metric"
    )
    p_summary.add_argument("--config", required=True)
    p_summary.add_argument("--run", help="run UUID (default: latest)")
    p_summary.set_defaults(func=_cmd_summary)

    p_check = sub.add_parser("check", help="validate config, tools, providers, results DB")
    p_check.add_argument("--config", required=True)
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
