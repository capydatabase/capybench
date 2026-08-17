"""Static chart generation from stored samples.

Reads ``bench_sample`` and builds one matplotlib figure per scenario. Two consumers:

* ``capybench chart`` writes each figure as a self-contained SVG into an output dir.
* ``capybench report`` embeds the same figures as PNG data URIs in a single HTML file.

No Grafana, no live server. One run's ``run_id`` selects which data to plot (defaults to
the latest run).

Every query filters ``vantage = 'client'``. Host-vantage readings answer a different
question with different units and belong in the report's host section, never silently
mixed onto an axis a reader will assume is customer-reproducible.

Palette is intentionally minimal, theme-neutral, and colorblind-safe.
"""

from __future__ import annotations

import io
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: render straight to file
import matplotlib.pyplot as plt
import psycopg

_PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#64748b"]


def latest_run_id(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "select id::text from bench_run order by started_at desc limit 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("no bench_run rows found; run a benchmark first")
    return row[0]


def build_figures(conn: psycopg.Connection, run_id: str) -> list[tuple[str, plt.Figure]]:
    """Build every scenario figure that has data for a run.

    Callers own the returned figures and must close them (``plt.close``) after use.
    """
    builders: tuple[tuple[str, Callable[[psycopg.Connection, str], plt.Figure | None]], ...] = (
        ("query_latency", _chart_query_latency),
        ("connection_ceiling", _chart_connection_ceiling),
        ("cold_cache", _chart_cold_cache),
        ("throughput", _chart_throughput),
        ("noisy_neighbor", _chart_noisy_neighbor),
        ("sustained", _chart_sustained),
        ("vacuum_under_write", _chart_vacuum_under_write),
        ("branch_speed", _chart_branch_speed),
        ("restore_rto", _chart_restore_rto),
        ("provision_speed", _chart_provision_speed),
        ("cold_start", _chart_cold_start),
    )
    figures: list[tuple[str, plt.Figure]] = []
    for name, builder in builders:
        fig = builder(conn, run_id)
        if fig is not None:
            figures.append((name, fig))
    return figures


def render_all(dsn: str, out_dir: str | Path, *, run_id: str | None = None) -> list[Path]:
    """Render every scenario chart for a run as SVG files. Returns the written paths."""
    run = run_id or latest_run_id(dsn)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with psycopg.connect(dsn) as conn:
        for name, fig in build_figures(conn, run):
            path = out / f"{name}.svg"
            fig.savefig(path, format="svg", bbox_inches="tight")
            plt.close(fig)
            written.append(path)
    return written


def figure_to_png(fig: plt.Figure, *, dpi: int = 144) -> bytes:
    """Rasterize a figure to PNG bytes and close it (for data-URI embedding)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _rows(conn: psycopg.Connection, run_id: str, scenario: str, metric: str) -> list[tuple]:
    return conn.execute(
        """
        select target, concurrency, dataset_bytes, variant, value, target_tier_usd
        from bench_sample
        where run_id = %s and scenario = %s and metric = %s and vantage = 'client'
        order by target, concurrency, dataset_bytes
        """,
        (run_id, scenario, metric),
    ).fetchall()


def _series_over_time(
    conn: psycopg.Connection, run_id: str, scenario: str, metric: str
) -> dict[str, list[tuple[float, float]]]:
    """``{target: [(elapsed_s, value)]}`` for scenarios that store a window time series."""
    rows = conn.execute(
        """
        select target, (params->>'elapsed_s')::float, value
        from bench_sample
        where run_id = %s and scenario = %s and metric = %s
          and variant = 'window' and vantage = 'client'
          and params -> 'elapsed_s' is not null
        order by target, 2
        """,
        (run_id, scenario, metric),
    ).fetchall()
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for target, elapsed, value in rows:
        out[target].append((float(elapsed), float(value)))
    return out


def _grouped_bars(
    ax: plt.Axes,
    targets: list[str],
    series: list[tuple[str, list[float]]],
    *,
    ylabel: str,
    title: str,
) -> None:
    """One group of bars per target, one bar per series - the shared comparison shape."""
    width = 0.8 / max(len(series), 1)
    for j, (label, values) in enumerate(series):
        xs = [i + j * width for i in range(len(targets))]
        ax.bar(xs, values, width, label=label, color=_color(j))
    ax.set_xticks([i + (len(series) - 1) * width / 2 for i in range(len(targets))])
    ax.set_xticklabels(targets, rotation=15, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)


def _color(idx: int) -> str:
    return _PALETTE[idx % len(_PALETTE)]


def _chart_query_latency(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    rows = conn.execute(
        """
        select target, variant, metric, value
        from bench_sample
        where run_id = %s and scenario = 'query_latency' and vantage = 'client'
          and metric in ('p50_ms', 'p95_ms', 'p99_ms')
        order by target, variant, metric
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return None

    # {variant: {target: {metric: value}}}
    data: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for target, variant, metric, value in rows:
        data[variant][target][metric] = value

    panels = [v for v in ("point-select", "select-1", "fresh-connect") if v in data]
    fig, axes = plt.subplots(1, len(panels), figsize=(5.5 * len(panels), 5))
    if len(panels) == 1:
        axes = [axes]

    metrics = ("p50_ms", "p95_ms", "p99_ms")
    for ax, variant in zip(axes, panels, strict=True):
        targets = sorted(data[variant])
        width = 0.8 / len(metrics)
        for j, metric in enumerate(metrics):
            xs = [i + j * width for i in range(len(targets))]
            ys = [data[variant][t].get(metric, 0.0) for t in targets]
            ax.bar(xs, ys, width, label=metric.removesuffix("_ms"), color=_color(j))
        ax.set_xticks([i + width for i in range(len(targets))])
        ax.set_xticklabels(targets, rotation=15, ha="right")
        ax.set_ylabel("latency (ms)")
        ax.set_title(variant)
        ax.legend()
        ax.grid(True, axis="y", alpha=0.3)

    fig.suptitle("Single-query latency (warm connection) & fresh-connect")
    return fig


def _chart_throughput(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
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
    return fig


def _chart_noisy_neighbor(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    rows = conn.execute(
        """
        select target, variant, value
        from bench_sample
        where run_id = %s and scenario = 'noisy_neighbor' and vantage = 'client'
          and metric like 'p%%_latency_ms'
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
    ax.set_title("Noisy-neighbor isolation - victim tail latency, idle vs under attack")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    return fig


def _chart_branch_speed(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
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
    return fig


def _chart_sustained(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    """Throughput over the whole run: the shape a 60-second benchmark cannot show."""
    series = _series_over_time(conn, run_id, "sustained", "tps")
    if not series:
        return None

    fig, ax = plt.subplots(figsize=(11, 5))
    for idx, (target, points) in enumerate(sorted(series.items())):
        xs = [p[0] / 60.0 for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, marker=".", label=target, color=_color(idx))
        # A reference line at the opening quarter makes drift readable at a glance
        # instead of requiring the eye to integrate the whole curve.
        quarter = max(len(ys) // 4, 1)
        baseline = sum(ys[:quarter]) / quarter
        ax.axhline(baseline, color=_color(idx), alpha=0.35, linestyle="--", linewidth=1)
    ax.set_xlabel("elapsed (minutes)")
    ax.set_ylabel("TPS per window")
    ax.set_ylim(bottom=0)
    ax.set_title("Sustained throughput - dashed line = first-quarter average")
    ax.legend()
    ax.grid(True, alpha=0.3)
    return fig


def _chart_connection_ceiling(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    rows = _rows(conn, run_id, "connection_ceiling", "connect_ms")
    if not rows:
        return None

    by_target: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for target, conc, _bytes, _variant, value, _tier in rows:
        if conc is not None:
            by_target[target].append((conc, value))

    fig, ax = plt.subplots(figsize=(9, 5))
    for idx, (target, points) in enumerate(sorted(by_target.items())):
        points.sort()
        ax.plot(*zip(*points, strict=True), marker=".", label=target, color=_color(idx))
        ax.axvline(points[-1][0], color=_color(idx), alpha=0.35, linestyle="--", linewidth=1)
    ax.set_xlabel("open connections")
    ax.set_ylabel("time to establish connection (ms)")
    ax.set_title("Connection ramp - dashed line = last connection accepted")
    ax.legend()
    ax.grid(True, alpha=0.3)
    return fig


def _chart_cold_cache(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    rows = conn.execute(
        """
        select target, variant, metric, value
        from bench_sample
        where run_id = %s and scenario = 'cold_cache' and vantage = 'client'
          and metric in ('scan_ms', 'buffer_hit_ratio_pct')
        order by target, variant
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return None

    data: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for target, variant, metric, value in rows:
        data[metric][target][variant] = value

    targets = sorted(data.get("scan_ms", {}))
    if not targets:
        return None

    fig, (ax_ms, ax_hit) = plt.subplots(1, 2, figsize=(12, 5))
    _grouped_bars(
        ax_ms,
        targets,
        [
            (variant, [data["scan_ms"][t].get(variant, 0.0) for t in targets])
            for variant in ("cold", "warm")
        ],
        ylabel="full scan (ms)",
        title="Scan time: buffer miss vs cached",
    )
    _grouped_bars(
        ax_hit,
        targets,
        [
            (
                variant,
                [
                    data.get("buffer_hit_ratio_pct", {}).get(t, {}).get(variant, 0.0)
                    for t in targets
                ],
            )
            for variant in ("cold", "warm")
        ],
        ylabel="buffer hit ratio (%)",
        title="Did the 'cold' scan really miss? (lower = colder)",
    )
    ax_hit.set_ylim(0, 100)
    fig.suptitle("Cold-cache penalty, with the eviction check that validates it")
    return fig


def _chart_vacuum_under_write(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    dead = _series_over_time(conn, run_id, "vacuum_under_write", "dead_tuple_pct")
    size = _series_over_time(conn, run_id, "vacuum_under_write", "table_bytes")
    if not dead and not size:
        return None

    fig, (ax_dead, ax_size) = plt.subplots(1, 2, figsize=(12, 5))
    for idx, (target, points) in enumerate(sorted(dead.items())):
        ax_dead.plot(
            [p[0] / 60.0 for p in points],
            [p[1] for p in points],
            marker=".",
            label=target,
            color=_color(idx),
        )
    ax_dead.set_xlabel("elapsed (minutes)")
    ax_dead.set_ylabel("dead tuples (% of all tuples)")
    ax_dead.set_title("Is autovacuum keeping up?")
    ax_dead.legend()
    ax_dead.grid(True, alpha=0.3)

    for idx, (target, points) in enumerate(sorted(size.items())):
        ax_size.plot(
            [p[0] / 60.0 for p in points],
            [p[1] / (1024 * 1024) for p in points],
            marker=".",
            label=target,
            color=_color(idx),
        )
    ax_size.set_xlabel("elapsed (minutes)")
    ax_size.set_ylabel("total relation size (MB)")
    ax_size.set_title("Permanent bloat under sustained writes")
    ax_size.legend()
    ax_size.grid(True, alpha=0.3)

    fig.suptitle("Vacuum behavior under sustained write load")
    return fig


def _chart_restore_rto(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    rows = _rows(conn, run_id, "restore_rto", "restore_rto_ms")
    if not rows:
        return None

    by_target: dict[str, list[float]] = defaultdict(list)
    sizes: dict[str, int] = {}
    for target, _conc, dataset_bytes, _variant, value, _tier in rows:
        by_target[target].append(value)
        if dataset_bytes:
            sizes[target] = int(dataset_bytes)

    targets = sorted(by_target)
    fig, ax = plt.subplots(figsize=(8, 5))
    values = [sum(by_target[t]) / len(by_target[t]) / 1000.0 for t in targets]
    ax.bar(range(len(targets)), values, 0.5, color=_color(0))
    ax.set_xticks(range(len(targets)))
    ax.set_xticklabels(
        [f"{t}\n({sizes[t] / (1024 * 1024):.0f} MB)" if t in sizes else t for t in targets],
        rotation=15,
        ha="right",
    )
    ax.set_ylabel("restore -> first query answered (s)")
    ax.set_title("Recovery time objective (mean of repeats)")
    ax.grid(True, axis="y", alpha=0.3)
    return fig


def _chart_provision_speed(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
    metrics = ("provision_api_ms", "provision_ready_ms", "destroy_ms")
    data: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for metric in metrics:
        for target, _conc, _bytes, _variant, value, _tier in _rows(
            conn, run_id, "provision_speed", metric
        ):
            data[metric][target].append(value)
    targets = sorted({t for by_target in data.values() for t in by_target})
    if not targets:
        return None

    def mean(metric: str, target: str) -> float:
        values = data[metric].get(target, [])
        return sum(values) / len(values) / 1000.0 if values else 0.0

    fig, ax = plt.subplots(figsize=(9, 5))
    _grouped_bars(
        ax,
        targets,
        [(metric.removesuffix("_ms"), [mean(metric, t) for t in targets]) for metric in metrics],
        ylabel="seconds",
        title="Create a database, use it, throw it away (mean of repeats)",
    )
    return fig


def _chart_cold_start(conn: psycopg.Connection, run_id: str) -> plt.Figure | None:
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
    return fig
