"""CapyBench command-line entrypoint.

    capybench run   --config capybench.toml [--only throughput,cold_start] [--notes "..."]
    capybench chart --config capybench.toml [--run <uuid>] [--out charts/]
    capybench check --config capybench.toml         # validate config + tool availability
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable

from . import __version__, charts, config
from .config import Suite
from .scenarios import branch_speed, cold_start, noisy_neighbor, throughput
from .store import Run

_SCENARIOS: dict[str, Callable[[Suite, Run], None]] = {
    "throughput": lambda s, r: throughput.run(s, r, log=_log),
    "noisy_neighbor": lambda s, r: noisy_neighbor.run(s, r, log=_log),
    "branch_speed": lambda s, r: branch_speed.run(s, r, log=_log),
    "cold_start": lambda s, r: cold_start.run(s, r, log=_log),
}


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


def _cmd_run(args: argparse.Namespace) -> int:
    suite = config.load(args.config)
    only = set(args.only.split(",")) if args.only else set(_SCENARIOS)
    unknown = only - set(_SCENARIOS)
    if unknown:
        _log(f"unknown scenario(s): {', '.join(sorted(unknown))}")
        return 2

    with Run.start(suite.results_dsn, git_sha=_git_sha(), notes=args.notes or suite.notes) as run:
        _log(f"run {run.run_id} started (scenarios: {', '.join(sorted(only))})")
        for name in ("throughput", "noisy_neighbor", "branch_speed", "cold_start"):
            if name in only:
                _SCENARIOS[name](suite, run)
        _log(f"run {run.run_id} complete")
    return 0


def _cmd_chart(args: argparse.Namespace) -> int:
    suite = config.load(args.config)
    paths = charts.render_all(suite.results_dsn, args.out, run_id=args.run)
    if not paths:
        _log("no samples matched; nothing rendered")
        return 1
    for path in paths:
        _log(f"wrote {path}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    suite = config.load(args.config)
    ok = True

    _log(f"config OK: {len(suite.targets)} target(s), results_dsn set")
    for tool in ("pgbench", "sysbench", "psql"):
        found = shutil.which(tool)
        _log(f"  {tool}: {'found at ' + found if found else 'MISSING'}")
        if tool != "psql" and not found:
            ok = False

    import psycopg

    try:
        with psycopg.connect(suite.results_dsn, connect_timeout=5) as conn:
            conn.execute("select 1 from bench_run limit 1")
        _log("  results DB: reachable, schema present")
    except psycopg.Error as exc:
        _log(f"  results DB: NOT ready ({exc}); apply sql/001_results_schema.sql")
        ok = False

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capybench", description=__doc__)
    parser.add_argument("--version", action="version", version=f"capybench {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run benchmark scenarios")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--only", help="comma-separated scenario subset")
    p_run.add_argument("--notes", help="free-text note stored on the run")
    p_run.set_defaults(func=_cmd_run)

    p_chart = sub.add_parser("chart", help="render SVG charts from stored samples")
    p_chart.add_argument("--config", required=True)
    p_chart.add_argument("--run", help="run UUID (default: latest)")
    p_chart.add_argument("--out", default="charts")
    p_chart.set_defaults(func=_cmd_chart)

    p_check = sub.add_parser("check", help="validate config, tools, and results DB")
    p_check.add_argument("--config", required=True)
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
