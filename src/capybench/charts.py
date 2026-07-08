"""Static chart generation from stored samples.

Reads ``bench_sample`` and emits one self-contained SVG per scenario into an output dir.
No Grafana, no live server — these are the files you drop into a deck, the marketing site,
or a PR. One run's ``run_id`` selects which data to plot (defaults to the latest run).

Palette is intentionally minimal and theme-neutral; swap the hex values for brand colors
when wiring into the site.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: render straight to file
import matplotlib.pyplot as plt
import psycopg

# Brand-neutral, colorblind-safe categorical palette. Replace with CapyDB brand tokens.
_PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#64748b"]


def latest_run_id(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select id::text from bench_run order by started_at desc limit 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("no bench_run rows found; run a benchmark first")
    return row[0]


def render_all(dsn: str, out_dir: str | Path, *, run_id: str | None = None) -> list[Path]:
    """Render every scenario chart for a run. Returns the written file paths."""
    run = run_id or latest_run_id(dsn)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with psycopg.connect(dsn) as conn:
        renderers = (
            ("throughput", _chart_throughput),
            ("noisy_neighbor", _chart_noisy_neighbor),
            ("branch_speed", _chart_branch_speed),
            ("cold_start", _chart_cold_start),
        )
        for _scenario, renderer in renderers:
            path = renderer(conn, run, out)
            if path is not None:
                written.append(path)
    return written


def _rows(conn: psycopg.Connection, run_id: str, scenario: str, metric: str) -> list[tuple]:
    return conn.execute(
        """
        select target, concurrency, dataset_bytes, variant, value, target_tier_usd
        from bench_sample
        where run_id = %s and scenario = %s and metric = %s
        order by target, concurrency, dataset_bytes
        """,
        (run_id, scenario, metric),
    ).fetchall()


def _color(idx: int) -> str:
    return _PALETTE[idx % len(_PALETTE)]


def _save(fig: plt.Figure, out: Path, name: str) -> Path:
    path = out / name
    fig.savefig(path, format="svg", bbox_inches="tight")
    plt.close(fig)
    return path


def _chart_throughput(conn: psycopg.Connection, run_id: str, out: Path) -> Path | None:
    rows = _rows(conn, run_id, "throughput", "p99_latency_ms")
    tps_rows = _rows(conn, run_id, "throughput", "tps")
    if not rows and not tps_rows:
        return None

    fig, (ax_tps, ax_lat) = plt.subplots(1, 2, figsize=(12, 5))

    by_target: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for target, conc, _bytes, _variant, value, _tier in tps_rows:
        if conc is not None:
            by_target[target].append((conc, value))
    for idx, (target, pts) in enumerate(sorted(by_target.items())):
        pts.sort()
        ax_tps.plot(*zip(*pts, strict=True), marker="o", label=target, color=_color(idx))
    ax_tps.set_title("Throughput vs concurrency")
    ax_tps.set_xlabel("clients")
    ax_tps.set_ylabel("TPS (pgbench)")
    ax_tps.legend()
    ax_tps.grid(True, alpha=0.3)

    by_target_lat: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for target, conc, _bytes, _variant, value, _tier in rows:
        if conc is not None:
            by_target_lat[target].append((conc, value))
    for idx, (target, pts) in enumerate(sorted(by_target_lat.items())):
        pts.sort()
        ax_lat.plot(*zip(*pts, strict=True), marker="o", label=target, color=_color(idx))
    ax_lat.set_title("p99 latency vs concurrency")
    ax_lat.set_xlabel("clients")
    ax_lat.set_ylabel("p99 latency (ms, sysbench)")
    ax_lat.legend()
    ax_lat.grid(True, alpha=0.3)

    fig.suptitle("OLTP throughput & tail latency")
    return _save(fig, out, "throughput.svg")


def _chart_noisy_neighbor(conn: psycopg.Connection, run_id: str, out: Path) -> Path | None:
    rows = conn.execute(
        """
        select target, variant, value
        from bench_sample
        where run_id = %s and scenario = 'noisy_neighbor' and metric like 'p%%_latency_ms'
        order by target, variant
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return None

    order = ["victim-alone", "victim-under-attack"]
    by_target: dict[str, dict[str, float]] = defaultdict(dict)
    for target, variant, value in rows:
        by_target[target][variant] = value

    targets = sorted(by_target)
    fig, ax = plt.subplots(figsize=(8, 5))
    width = 0.35
    for j, variant in enumerate(order):
        xs = [i + j * width for i in range(len(targets))]
        ys = [by_target[t].get(variant, 0.0) for t in targets]
        ax.bar(xs, ys, width, label=variant, color=_color(j))
    ax.set_xticks([i + width / 2 for i in range(len(targets))])
    ax.set_xticklabels(targets)
    ax.set_ylabel("tail latency (ms)")
    ax.set_title("Noisy-neighbor isolation — victim tail latency, idle vs under attack")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    return _save(fig, out, "noisy_neighbor.svg")


def _chart_branch_speed(conn: psycopg.Connection, run_id: str, out: Path) -> Path | None:
    rows = _rows(conn, run_id, "branch_speed", "branch_create_ms")
    if not rows:
        return None

    # Average repeats per (target, size).
    agg: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for target, _conc, dataset_bytes, _variant, value, _tier in rows:
        if dataset_bytes is not None:
            agg[target][int(dataset_bytes)].append(value)

    fig, ax = plt.subplots(figsize=(8, 5))
    for idx, (target, by_size) in enumerate(sorted(agg.items())):
        xs = sorted(by_size)
        ys = [sum(by_size[s]) / len(by_size[s]) for s in xs]
        xs_gb = [s / (1024**3) for s in xs]
        ax.plot(xs_gb, ys, marker="o", label=target, color=_color(idx))
    ax.set_xlabel("parent size (GB)")
    ax.set_ylabel("branch create → connectable (ms)")
    ax.set_title("Branch creation time vs parent size")
    ax.legend()
    ax.grid(True, alpha=0.3)
    return _save(fig, out, "branch_speed.svg")


def _chart_cold_start(conn: psycopg.Connection, run_id: str, out: Path) -> Path | None:
    rows = _rows(conn, run_id, "cold_start", "cold_start_ms")
    if not rows:
        return None

    by_target: dict[str, list[float]] = defaultdict(list)
    for target, _conc, _bytes, _variant, value, _tier in rows:
        by_target[target].append(value)

    fig, ax = plt.subplots(figsize=(8, 5))
    for idx, (target, values) in enumerate(sorted(by_target.items())):
        ordered = sorted(values)
        n = len(ordered)
        cdf = [(i + 1) / n for i in range(n)]
        ax.plot(ordered, cdf, marker=".", label=f"{target} (n={n})", color=_color(idx))
    ax.set_xlabel("cold-start wake latency (ms)")
    ax.set_ylabel("cumulative fraction")
    ax.set_title("Scale-to-zero cold-start latency (CDF)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    return _save(fig, out, "cold_start.svg")
