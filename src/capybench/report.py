"""Self-contained HTML report generation.

``capybench report`` produces a single HTML file - inline CSS, charts embedded as PNG
data URIs, no external requests - from one stored run. The report is meant to be the
artifact you attach to a results PR or share directly, so it leads with reproducibility
metadata: client machine, client region, and an echo of the (secret-free) suite config.
"""

from __future__ import annotations

import base64
import dataclasses
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from . import __version__, charts
from .config import Suite

_CHART_TITLES = {
    "query_latency": "Single-query & connection latency",
    "connection_ceiling": "Connection ceiling",
    "cold_cache": "Cold-cache penalty",
    "throughput": "Throughput & tail latency",
    "noisy_neighbor": "Noisy-neighbor isolation",
    "sustained": "Sustained throughput (drift)",
    "vacuum_under_write": "Vacuum under sustained writes",
    "branch_speed": "Branch creation speed",
    "restore_rto": "Restore time (RTO)",
    "provision_speed": "Provisioning speed",
    "cold_start": "Cold-start wake latency",
}

#: Fact categories worth leading with, in the order a reader should meet them.
_FACT_CATEGORY_ORDER = (
    "durability",
    "server",
    "resources",
    "pooling",
    "roles",
    "gucs",
    "extensions",
    "connections",
    "planner",
    "policy",
    "other",
)

_CSS = """
:root { color-scheme: light; }
body { font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0 auto; max-width: 960px; padding: 2rem 1.25rem 4rem; color: #1f2937; }
h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.15rem; margin-top: 2.25rem; border-bottom: 1px solid #e5e7eb;
     padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; margin: 0.75rem 0; font-size: 0.86rem; }
th, td { border: 1px solid #e5e7eb; padding: 0.35rem 0.55rem; text-align: left;
         vertical-align: top; }
th { background: #f8fafc; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code { background: #f1f5f9; padding: 0.1rem 0.3rem; border-radius: 3px;
       font-size: 0.85em; }
img.chart { max-width: 100%; height: auto; border: 1px solid #e5e7eb;
            border-radius: 6px; margin: 0.5rem 0; }
.meta { color: #64748b; font-size: 0.9rem; }
.warn { background: #fef3c7; border: 1px solid #fcd34d; border-radius: 6px;
        padding: 0.6rem 0.9rem; margin: 0.75rem 0; font-size: 0.9rem; }
.scroll { overflow-x: auto; }
footer { margin-top: 3rem; color: #94a3b8; font-size: 0.8rem; }
"""


def render(suite: Suite, out_path: str | Path, *, run_id: str | None = None) -> Path:
    """Render one run into a self-contained HTML file. Returns the written path."""
    run = run_id or charts.latest_run_id(suite.results_dsn)
    with psycopg.connect(suite.results_dsn) as conn:
        meta = _run_meta(conn, run)
        images = [
            (name, base64.b64encode(charts.figure_to_png(fig)).decode("ascii"))
            for name, fig in charts.build_figures(conn, run)
        ]
        summary = _summary_rows(conn, run)
        facts = _fact_rows(conn, run)
        host = _host_rows(conn, run)

    path = Path(out_path)
    path.write_text(_render_html(suite, meta, images, summary, facts, host), encoding="utf-8")
    return path


def _run_meta(conn: psycopg.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        select id::text, harness_version, git_sha, started_at, finished_at, notes, client
        from bench_run where id = %s
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"run {run_id!r} not found in bench_run")
    return {
        "id": row[0],
        "harness_version": row[1],
        "git_sha": row[2],
        "started_at": row[3],
        "finished_at": row[4],
        "notes": row[5],
        "client": _as_dict(row[6]),
        "attestation": _as_dict(row[7]),
    }


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, str):  # older schema stored via text casting
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _summary_rows(conn: psycopg.Connection, run_id: str) -> list[tuple]:
    return conn.execute(
        """
        select scenario, metric, target, unit, count(*),
               min(value),
               percentile_cont(0.5) within group (order by value),
               max(value)
        from bench_sample
        where run_id = %s and vantage = 'client'
        group by scenario, metric, target, unit
        order by scenario, metric, target
        """,
        (run_id,),
    ).fetchall()


def _fact_rows(conn: psycopg.Connection, run_id: str) -> list[tuple]:
    return conn.execute(
        """
        select category, key, target, value, source
        from bench_fact
        where run_id = %s and vantage = 'client'
        order by category, key, target
        """,
        (run_id,),
    ).fetchall()


def _host_rows(conn: psycopg.Connection, run_id: str) -> dict[str, list[tuple]]:
    """Host-vantage readings, kept in their own structure so they cannot leak into charts."""
    samples = conn.execute(
        """
        select scenario, target, metric, unit, value
        from bench_sample
        where run_id = %s and vantage = 'host' and variant = 'delta'
        order by scenario, metric
        """,
        (run_id,),
    ).fetchall()
    facts = conn.execute(
        """
        select target, key, value
        from bench_fact
        where run_id = %s and vantage = 'host'
        order by key
        """,
        (run_id,),
    ).fetchall()
    return {"samples": samples, "facts": facts}


def _esc(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return html.escape(value.isoformat(sep=" ", timespec="seconds"))
    return html.escape(str(value))


def _num(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


def _kv_table(pairs: list[tuple[str, object]]) -> str:
    rows = "".join(f"<tr><th>{_esc(key)}</th><td>{_esc(val)}</td></tr>" for key, val in pairs)
    return f'<div class="scroll"><table>{rows}</table></div>'


def _targets_table(suite: Suite) -> str:
    header = (
        "<tr><th>target</th><th>host</th><th>port</th><th>dbname</th><th>sslmode</th>"
        "<th>tier $/mo</th><th>pg</th><th>region</th><th>provider</th><th>project</th></tr>"
    )
    body = "".join(
        "<tr>"
        f"<td>{_esc(t.name)}</td><td>{_esc(t.host)}</td><td class='num'>{t.port}</td>"
        f"<td>{_esc(t.dbname)}</td><td>{_esc(t.sslmode)}</td>"
        f"<td class='num'>{_esc(t.tier_usd)}</td><td>{_esc(t.pg_version)}</td>"
        f"<td>{_esc(t.region)}</td><td>{_esc(t.provider)}</td><td>{_esc(t.project)}</td>"
        "</tr>"
        for t in suite.targets.values()
    )
    return f'<div class="scroll"><table>{header}{body}</table></div>'


def _facts_table(facts: list[tuple]) -> str:
    """Pivot facts into one column per target - the side-by-side comparison view.

    Facts are what make the numbers comparable, so they are rendered as a matrix a reader
    can scan for differences rather than a flat list per target.
    """
    if not facts:
        return (
            "<p class='meta'>No fact sheet for this run. Add a <code>[fact_sheet]</code> "
            "block: published numbers should always carry the configuration that produced "
            "them.</p>"
        )

    targets = sorted({row[2] for row in facts})
    by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for category, key, target, value, source in facts:
        by_key.setdefault((category or "other", key, source), {})[target] = value

    def order(item: tuple[tuple[str, str, str], dict[str, str]]) -> tuple[int, str, str]:
        category, key, _source = item[0]
        rank = (
            _FACT_CATEGORY_ORDER.index(category)
            if category in _FACT_CATEGORY_ORDER
            else len(_FACT_CATEGORY_ORDER)
        )
        return rank, category, key

    header = (
        "<tr><th>category</th><th>fact</th>"
        + "".join(f"<th>{_esc(t)}</th>" for t in targets)
        + "<th>source</th></tr>"
    )
    body: list[str] = []
    for (category, key, source), values in sorted(by_key.items(), key=order):
        cells = "".join(f"<td>{_esc(values.get(t, '-'))}</td>" for t in targets)
        body.append(
            f"<tr><td>{_esc(category)}</td><th>{_esc(key)}</th>{cells}"
            f"<td class='meta'>{_esc(source)}</td></tr>"
        )
    return f'<div class="scroll"><table>{header}{"".join(body)}</table></div>'


def _attestation_section(attestation: dict[str, Any]) -> str:
    if not attestation:
        return ""
    rows = "".join(
        f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>"
        for key, value in sorted(attestation.items())
    )
    return (
        "<h2>Attestation (declared, not measured)</h2>"
        '<div class="warn">These statements were declared by whoever ran this benchmark. '
        "The harness cannot verify them - it has no way to confirm, for example, that two "
        "targets really share a physical node. Treat them as claims that give the numbers "
        "context, and weigh them accordingly.</div>"
        f'<div class="scroll"><table>{rows}</table></div>'
    )


def _host_section(host: dict[str, list[tuple]]) -> str:
    """Host-vantage readings, explicitly separated from customer-reproducible numbers."""
    samples, facts = host.get("samples", []), host.get("facts", [])
    if not samples and not facts:
        return ""

    parts = [
        "<h2>Host vantage (privileged)</h2>",
        '<div class="warn">These readings come from the platform\'s own infrastructure '
        "via a host probe, not through a client connection. A customer cannot reproduce "
        "them, and they are never plotted alongside client measurements - they exist to "
        "corroborate (or contradict) what the client-side numbers imply.</div>",
    ]
    if facts:
        rows = "".join(
            f"<tr><th>{_esc(key)}</th><td>{_esc(target)}</td><td>{_esc(value)}</td></tr>"
            for target, key, value in facts
        )
        parts.append(
            "<h3>Node</h3><div class='scroll'><table>"
            f"<tr><th>fact</th><th>node</th><th>value</th></tr>{rows}</table></div>"
        )
    if samples:
        rows = "".join(
            f"<tr><td>{_esc(scenario)}</td><td>{_esc(target)}</td><th>{_esc(metric)}</th>"
            f"<td class='num'>{_num(value)}</td><td>{_esc(unit)}</td></tr>"
            for scenario, target, metric, unit, value in samples
        )
        parts.append(
            "<h3>Change during each scenario</h3><div class='scroll'><table>"
            "<tr><th>scenario</th><th>node</th><th>counter</th><th>delta</th><th>unit</th></tr>"
            f"{rows}</table></div>"
        )
    return "".join(parts)


def _scenario_config_table(suite: Suite) -> str:
    from .scenarios import NAMES

    rows: list[str] = []
    for name in NAMES:
        cfg = getattr(suite, name)
        if cfg is None:
            rows.append(f"<tr><th>{name}</th><td>not configured</td></tr>")
            continue
        knobs = ", ".join(
            f"{field.name}={getattr(cfg, field.name)!r}" for field in dataclasses.fields(cfg)
        )
        rows.append(f"<tr><th>{name}</th><td><code>{_esc(knobs)}</code></td></tr>")
    return f'<div class="scroll"><table>{"".join(rows)}</table></div>'


def _providers_table(suite: Suite) -> str:
    if not suite.providers:
        return "<p class='meta'>No [providers.*] blocks; all targets use built-in defaults.</p>"
    rows = "".join(
        f"<tr><th>{_esc(cfg.name)}</th><td>{_esc(cfg.type)}</td>"
        f"<td><code>{_esc(json.dumps(cfg.settings, default=str))}</code></td></tr>"
        for cfg in suite.providers.values()
    )
    header = "<tr><th>name</th><th>type</th><th>settings</th></tr>"
    return f'<div class="scroll"><table>{header}{rows}</table></div>'


def _render_html(
    suite: Suite,
    meta: dict[str, Any],
    images: list[tuple[str, str]],
    summary: list[tuple],
    facts: list[tuple],
    host: dict[str, list[tuple]],
) -> str:
    client: dict[str, Any] = meta["client"]
    region = client.get("region") or suite.client_region

    region_block = (
        f"<p><strong>Client region:</strong> {_esc(region)}</p>"
        if region
        else (
            '<div class="warn"><strong>No client region recorded.</strong> '
            "Results are only comparable when the client is colocated (same region) with "
            "the targets; set <code>client_region</code> in the config and re-run before "
            "publishing.</div>"
        )
    )

    client_pairs = [(key, val) for key, val in sorted(client.items()) if key != "region"]
    client_table = (
        _kv_table(client_pairs)
        if client_pairs
        else "<p class='meta'>Client machine info was not recorded for this run "
        "(older harness version).</p>"
    )

    chart_sections = (
        "".join(
            f"<h2>{_esc(_CHART_TITLES.get(name, name))}</h2>"
            f'<img class="chart" alt="{_esc(name)} chart" '
            f'src="data:image/png;base64,{b64}">'
            for name, b64 in images
        )
        or "<h2>Charts</h2><p class='meta'>No samples found for this run.</p>"
    )

    summary_header = (
        "<tr><th>scenario</th><th>metric</th><th>target</th><th>unit</th><th>n</th>"
        "<th>min</th><th>median</th><th>max</th></tr>"
    )
    summary_body = "".join(
        "<tr>"
        f"<td>{_esc(scenario)}</td><td>{_esc(metric)}</td><td>{_esc(target)}</td>"
        f"<td>{_esc(unit)}</td><td class='num'>{n}</td>"
        f"<td class='num'>{_num(vmin)}</td><td class='num'>{_num(vmed)}</td>"
        f"<td class='num'>{_num(vmax)}</td>"
        "</tr>"
        for scenario, metric, target, unit, n, vmin, vmed, vmax in summary
    )

    run_pairs = [
        ("run id", meta["id"]),
        ("harness version", meta["harness_version"]),
        ("harness git sha", meta["git_sha"]),
        ("started at", meta["started_at"]),
        ("finished at", meta["finished_at"]),
        ("notes", meta["notes"]),
    ]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CapyBench report · run {_esc(meta["id"])[:8]}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>CapyBench report</h1>
<p class="meta">Provider-neutral managed-Postgres benchmark ·
run <code>{_esc(meta["id"])}</code></p>

<h2>Run</h2>
{_kv_table(run_pairs)}

<h2>Client machine &amp; reproducibility</h2>
{region_block}
{client_table}
<p class="meta">Passwords and API tokens are never included in this report; connection
secrets come from the environment, not the config.</p>

{_attestation_section(meta["attestation"])}

<h2>Suite configuration (echo)</h2>
<h3>Targets</h3>
{_targets_table(suite)}
<h3>Providers</h3>
{_providers_table(suite)}
<h3>Scenario knobs</h3>
{_scenario_config_table(suite)}

<h2>Fact sheet - what these numbers were measured under</h2>
<p class="meta">A throughput figure only means something next to the durability and
resource posture that produced it. <code>source=probe</code> rows were proven by
behavior; <code>pg_settings</code> rows are what the platform reports about itself.</p>
{_facts_table(facts)}

{chart_sections}

<h2>Sample summary (client vantage)</h2>
<div class="scroll"><table>{summary_header}{summary_body}</table></div>

{_host_section(host)}

<footer>Generated by capybench {_esc(__version__)} · single self-contained file, no
external resources.</footer>
</body>
</html>
"""
