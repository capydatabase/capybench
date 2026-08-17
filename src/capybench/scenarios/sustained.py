"""Scenario - does the throughput hold up over half an hour?

Nearly every published managed-Postgres benchmark runs for sixty seconds, which is
exactly the window in which a burstable instance looks fastest. Burst credits, IO
allowances and shared-vCPU scheduling all expire on a timescale a short run never
reaches, so the number an application lives with is not the number those benchmarks
print.

This scenario runs one fixed-concurrency workload for a long time and keeps every
``--progress`` window, so the *shape* survives: a flat line, a cliff at minute twelve, or
a saw-tooth around checkpoints. The headline metric is ``tps_drift_pct`` - how much the
last quarter of the run differs from the first.

Windows are stored individually (``variant = "window"``, elapsed seconds in ``params``)
so the chart is a time series, not an average that hides the cliff it is averaging over.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence

from ..config import Suite, Target
from ..runners import pgbench
from ..store import Run, Sample

SCENARIO = "sustained"


def run(suite: Suite, run: Run, *, log: Callable[[str], None]) -> None:
    cfg = suite.sustained
    if cfg is None:
        log("sustained: no [sustained] config; skipping")
        return

    for name in cfg.targets or tuple(suite.targets):
        target = suite.target(name)
        log(
            f"sustained: {name} - {cfg.duration_s}s at {cfg.concurrency} clients "
            f"({cfg.duration_s // max(cfg.window_s, 1)} windows)"
        )
        try:
            pgbench.initialize(target, scale=cfg.scale)
            result = pgbench.run(
                target,
                clients=cfg.concurrency,
                duration_s=cfg.duration_s,
                read_only=cfg.read_only,
                progress_s=cfg.window_s,
            )
        except RuntimeError as exc:
            log(f"sustained: target {name} FAILED, continuing with next: {exc}")
            continue

        _record(run, target, cfg.concurrency, result, log=log)


def _record(
    run: Run,
    target: Target,
    concurrency: int,
    result: pgbench.PgbenchResult,
    *,
    log: Callable[[str], None],
) -> None:
    def sample(metric: str, value: float, unit: str, **kwargs: object) -> None:
        run.record(
            Sample(
                scenario=SCENARIO,
                target=target.name,
                target_tier_usd=target.tier_usd,
                pg_version=target.pg_version,
                region=target.region,
                concurrency=concurrency,
                metric=metric,
                value=value,
                unit=unit,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    for window in result.progress:
        params = {"elapsed_s": window.elapsed_s}
        sample("tps", window.tps, "tps", variant="window", params=params)
        if window.latency_avg_ms is not None:
            sample("latency_avg_ms", window.latency_avg_ms, "ms", variant="window", params=params)

    sample("tps", result.tps, "tps", variant="overall")
    sample("latency_avg_ms", result.latency_avg_ms, "ms", variant="overall")

    tps_series = [window.tps for window in result.progress]
    if len(tps_series) < 4:
        log(
            f"sustained: {target.name} produced only {len(tps_series)} progress window(s); "
            f"drift needs at least 4 - lower window_s or raise duration_s"
        )
        return

    shape = drift(tps_series)
    for metric, unit in (
        ("tps_first_quarter", "tps"),
        ("tps_last_quarter", "tps"),
        ("tps_drift_pct", "pct"),
        ("tps_cv_pct", "pct"),
        ("tps_min_window", "tps"),
    ):
        sample(metric, shape[metric], unit, variant="shape")

    log(
        f"sustained: {target.name} {shape['tps_first_quarter']:.0f} -> "
        f"{shape['tps_last_quarter']:.0f} tps "
        f"(drift {shape['tps_drift_pct']:+.1f}%, cv {shape['tps_cv_pct']:.1f}%)"
    )


def drift(tps_series: Sequence[float]) -> dict[str, float]:
    """Shape metrics for a throughput series.

    ``tps_drift_pct`` compares the mean of the last quarter of windows with the first
    quarter: negative means the target slowed down over the run, which is the burst-credit
    signature. ``tps_cv_pct`` is the coefficient of variation across all windows - high
    values mean an unstable target even when the endpoints happen to match.

    Pure function of the series so the interpretation is testable and identical for every
    platform. Requires at least 4 windows (one per quarter).
    """
    if len(tps_series) < 4:
        raise ValueError(f"drift() needs at least 4 windows, got {len(tps_series)}")

    quarter = len(tps_series) // 4
    first = statistics.fmean(tps_series[:quarter])
    last = statistics.fmean(tps_series[-quarter:])
    mean = statistics.fmean(tps_series)
    stdev = statistics.pstdev(tps_series)
    return {
        "tps_first_quarter": first,
        "tps_last_quarter": last,
        "tps_drift_pct": 100.0 * (last - first) / first if first else 0.0,
        "tps_cv_pct": 100.0 * stdev / mean if mean else 0.0,
        "tps_min_window": min(tps_series),
    }
