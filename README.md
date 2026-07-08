# CapyBench

Competitive benchmark harness for CapyDB. Runs four scenarios chosen to exercise where the
instance-per-project / ZFS / scale-to-zero architecture actually differs from managed-PG
competitors, stores every measurement in Postgres, and renders static SVG charts you can
drop straight into a deck or the marketing site.

| Scenario | What it proves | Tool | Headline chart |
|---|---|---|---|
| `throughput` | "not slower at the same price" | pgbench + sysbench | TPS & p99 vs concurrency |
| `noisy_neighbor` | cgroup/dataset isolation | sysbench victim + pgbench aggressor | victim p99 idle vs under attack |
| `branch_speed` | ZFS CoW clone is O(1) in data size | control CLI + psycopg | branch time vs parent size |
| `cold_start` | socket-activated wake latency | control CLI + psycopg | wake latency CDF |

Raw single-instance throughput is table stakes — it's here as a sanity check, not the
pitch. The isolation / branching / cold-start charts are the differentiators.

## Install

```bash
cd benchmarks
uv sync
brew install sysbench        # pgbench ships with postgresql; sysbench is separate
```

## Set up the results DB

Dogfood a CapyDB project as the results store, then apply the schema:

```bash
psql "$CAPYBENCH_RESULTS_DSN" -f sql/001_results_schema.sql
```

## Configure

```bash
cp capybench.toml.example capybench.toml
# edit: results.dsn, targets, and control.capydb.commands (real `capydb` CLI invocations)
uv run capybench check --config capybench.toml     # validates config, tools, results DB
```

`control.*.commands` are shell templates the harness runs to drive lifecycle ops
(branch/sleep/wake). The harness never hardcodes control-plane API paths — it drives the
existing `capydb` CLI (and each competitor's CLI). Fill the templates with real commands
verified against `capydb --help`.

## Run

```bash
# CapyDB-only baseline first — get your own numbers before adding competitors:
uv run capybench run --config capybench.toml --only throughput,cold_start

# full matrix once targets are wired:
uv run capybench run --config capybench.toml --notes "baseline capydb-db1"

# render charts from the latest run:
uv run capybench chart --config capybench.toml --out charts/
```

## Methodology notes (put these on every published chart)

- **Compare at matching `tier_usd`**, not matching hardware — that's the buyer's real
  decision.
- Pin PG version, region, and client→server network path; record them (the harness tags
  every sample with `pg_version` / `region` / `tier_usd`).
- For `noisy_neighbor`, point `victim` and `aggressor` at two projects on the **same
  physical host** or the test is meaningless.
- Publish the config + methodology so results are reproducible. Be deliberate about naming
  competitors in published charts — some managed-PG terms of service restrict benchmark
  publication; "vs shared tenancy" as a category is safer than a named-vendor callout.

## Layout

```
sql/001_results_schema.sql     tall results schema (bench_run, bench_sample)
src/capybench/
  config.py                    TOML suite model + validation
  store.py                     Run / Sample persistence
  runners/pgbench.py           pgbench wrapper (TPS, avg latency, aggressor spawn)
  runners/sysbench.py          sysbench oltp wrapper (TPS, tail latency)
  control/commands.py          lifecycle ops via configured CLI templates
  scenarios/*.py               the four scenarios
  charts.py                    static SVG rendering (matplotlib, headless)
  cli.py                       run | chart | check
```

## Adding a competitor

1. Add a `[targets.<name>]` block with its connection fields and a matching `tier_usd`.
2. Add a `[control.<provider>.commands]` block if you want it in `branch_speed` /
   `cold_start` (throughput/noisy_neighbor need only a DSN).
3. Re-run. Charts group by target automatically.
