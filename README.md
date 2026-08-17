# CapyBench

A provider-neutral benchmark harness for managed Postgres. It measures the things that
actually differ between platforms - not just raw TPS - stores every measurement in a
Postgres results database, and renders charts and a self-contained HTML report you can
attach to a results PR.

CapyBench is maintained by [CapyDB](https://capydb.dev), but the harness itself is
provider-agnostic: any Postgres you can reach by DSN can be benchmarked, and platforms
with branching or scale-to-zero APIs plug in through a small provider adapter interface.
Community results and new provider adapters are welcome - see
[CONTRIBUTING.md](CONTRIBUTING.md).

## What it measures

Most of these need nothing but a DSN, so they run against **any** managed Postgres:

| Scenario | Question it answers | Tools |
|---|---|---|
| `fact_sheet` | what posture produced these numbers - durability, resources, role rights, extensions, whether a pooler is in the path, which GUCs you may set | psycopg |
| `query_latency` | what one query costs an app endpoint: warm round-trip, indexed point-select, and fresh TLS+auth connect (p50/p95/p99) | psycopg |
| `connection_ceiling` | how many connections you really get, and whether the wall is a refusal or a latency cliff | psycopg |
| `cold_cache` | what a buffer miss costs, with a hit-ratio check that proves the cache was actually cold | psycopg |
| `throughput` | OLTP throughput & tail latency at each price tier | pgbench + sysbench |
| `noisy_neighbor` | how much a co-located tenant's storm moves your p99 | sysbench victim + pgbench aggressor |
| `vacuum_under_write` | does autovacuum keep up under sustained writes, or does bloat become permanent | pgbench + psycopg |
| `sustained` | does throughput hold for half an hour, or fall off a cliff when burst credits run out | pgbench |

These need a provider adapter, because they drive the platform's own lifecycle API:

| Scenario | Question it answers | Capability |
|---|---|---|
| `branch_speed` | does branch/preview creation stay flat as data grows | `branch` |
| `cold_start` | wake-from-sleep latency as a client experiences it | `sleep` |
| `restore_rto` | how long from "restore" to a query answered - the number nobody publishes | `restore` |
| `provision_speed` | how long from "create me a database" to first query, and teardown | `provision` |

`throughput` additionally records exact p50/p95/p99 per-transaction percentiles parsed
from pgbench `-l` logs (`pgbench_p50_ms` etc.), alongside the single sysbench percentile.

Every sample is tagged with `tier_usd`, `pg_version`, and `region`, so charts compare
like-for-like: matching monthly price, not matching hardware - that is the buyer's real
decision.

## Two vantage points

Measurements come from one of two places, recorded on every row as `vantage` and never
mixed on a chart axis:

- **`client`** - taken through a normal connection. Anyone can reproduce it, which is what
  makes it worth publishing. This is the default and covers every scenario above.
- **`host`** - taken with privileged access on the node itself (cgroup CPU throttling, PSI
  pressure, cache hit ratios). Only the operator of a platform can produce these, and they
  exist to corroborate what the client numbers imply. Configure a `[host_probe]` block;
  the runner snapshots it around every scenario and records what changed.

The built-in `ssh` probe reads standard Linux interfaces (`/proc/pressure`, cgroup v2,
ZFS `arcstats`), so it is not tied to any platform - any operator with SSH to their node
can use it.

Anything the harness genuinely cannot check - that two targets really share a physical
node, what the node's hardware is, how many tenants it holds - goes in an `[attestation]`
block. It is published in the report labelled as **declared, not measured**. A claim with
a name on it beats an unstated assumption.

## Provider matrix

Scenarios needing a capability are skipped, with an explanatory message, when the provider
behind a target does not implement it. Nothing fails and nothing is faked:

| Provider type | provision | branch | sleep | restore |
|---|---|---|---|---|
| `generic` (any Postgres DSN) | - | - | - | - |
| `capydb` | yes | yes | yes | yes |

`generic` is the default and needs no configuration - point a target at any DSN, and the
eight DSN-only scenarios all run. Adapters for other platforms are welcome as PRs (see
"Adding a provider" below) - we deliberately do not ship half-implemented stubs.

## Quickstart

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/), `pgbench` (ships with
Postgres), `sysbench` built with the pgsql driver (`brew install sysbench` /
`apt-get install sysbench`).

```bash
uv sync

# 1. Point a Postgres database at the results schema (001 first, then each migration):
psql "$CAPYBENCH_RESULTS_DSN" -f sql/001_results_schema.sql
psql "$CAPYBENCH_RESULTS_DSN" -f sql/002_run_client.sql
psql "$CAPYBENCH_RESULTS_DSN" -f sql/003_facts_and_vantage.sql

# 2. Configure:
cp capybench.toml.example capybench.toml
#    edit: results.dsn, targets, client_region, [providers.*]

# 3. Validate config, tools, providers, and the results DB:
uv run capybench check --config capybench.toml

# 4. Run (start with a subset, then the full matrix):
uv run capybench run --config capybench.toml --only throughput
uv run capybench run --config capybench.toml --notes "baseline"

# 5. Render outputs from the latest run:
uv run capybench chart  --config capybench.toml --out charts/
uv run capybench report --config capybench.toml --out report.html
```

`capybench report` writes a single self-contained HTML file - inline CSS, charts embedded
as data URIs, no external requests - that includes the run's client-machine info, client
region, and a secret-free echo of the suite config, so published results carry their own
reproducibility metadata.

## Configuration

One TOML file drives everything (see `capybench.toml.example`):

- `[results]` - DSN of the Postgres where samples land.
- `client_region` - where the client machine running the harness lives. Always set it.
- `[targets.<name>]` - a connectable Postgres plus comparison metadata (`tier_usd`,
  `pg_version`, `region`) and an optional `provider` reference (default `generic`).
- `[providers.<name>]` - a provider adapter and its settings. `type` selects the
  implementation (defaults to the block name). API tokens are read from environment
  variables (`token_env`), never from the TOML, so configs stay committable.
- `[host_probe]` - optional privileged vantage point on the node (operators only).
- `[attestation]` - optional declared facts the harness cannot verify.
- One section per scenario (`[throughput]`, `[sustained]`, …) holding its knobs. **Omit a
  section to skip that scenario** - that is how you choose what a run does.

`capybench check` prints, before you spend an hour on a run, exactly which scenarios will
run, which will skip, and why.

## Methodology (read before publishing results)

- **Colocate the client.** Run the harness from a machine in the same region as the
  targets. Cross-region client latency swamps every signal here; results measured from a
  differently-located client are not comparable with anyone else's.
- **Compare at matching `tier_usd`**, not matching hardware.
- **Pin and publish** PG version, region, client machine spec, and the exact TOML used.
  The harness records client info and config echo per run; the HTML report surfaces them.
- **`noisy_neighbor` measures fair-share isolation**, not magic. Point `victim` and
  `aggressor` at two databases on the **same physical node**, or the test is meaningless.
  On a fair-share system some victim degradation under an aggressor storm is expected and
  correct - the metric is how far the tail moves, and how predictably.
- **`branch_speed` sweeps parent size** to distinguish copy-on-write branching (flat
  line) from snapshot-restore designs (time grows with data).
- **Publish the fact sheet with the numbers.** A throughput figure measured with
  `synchronous_commit=off` is not comparable with a durable one. `fact_sheet` records the
  posture and the HTML report puts it next to the charts; if two targets disagree on
  `durability_posture`, say so rather than putting their bars side by side.
- **`sustained` is where short benchmarks lie.** Sixty seconds is exactly the window in
  which a burstable instance looks fastest. Run at least 30 minutes before believing a
  throughput number, and report `tps_drift_pct`.
- **`cold_cache` measures the buffer-miss floor, not the disk.** Evicting shared_buffers
  does not evict the OS page cache or ZFS ARC, so the penalty is a lower bound. The
  scenario reports the hit ratio it actually achieved - if the "cold" scan still hit
  buffers, the number is understated and the run says so.
- **`connection_ceiling` deliberately walks a database to its limit.** Do not point it at
  production, and set `max_probe` consciously.
- **`restore_rto` scales with data.** Publish the source database size (the harness records
  it); only compare restores of similar-sized databases.
- **Repeat for distributions.** Single-shot numbers, especially cold-start, are noise;
  the harness records every repeat and charts CDFs.
- **Check the terms of service** before naming a vendor in published charts - some
  managed-Postgres ToS restrict benchmark publication. "vs shared tenancy" as a category
  is safer than a named-vendor callout when in doubt.

## Layout

```
sql/                           results schema (bench_run, bench_sample, bench_fact) + migrations
src/capybench/
  capabilities.py              Capability vocabulary shared by providers and scenarios
  config.py                    TOML suite model + validation
  store.py                     Run / Sample / Fact persistence, Vantage
  providers/                   provider adapter interface + implementations
    base.py                    Provider base class, capability set, wait_connectable
    generic.py                 any Postgres DSN (no lifecycle ops)
    capydb.py                  reference adapter: provision / branch / sleep / restore
  hostprobe/                   privileged (vantage=host) adapters
    base.py                    HostProbe base class, Reading, counter deltas
    ssh.py                     PSI / cgroup v2 / ZFS arcstats over one SSH round trip
  runners/pgbench.py           pgbench wrapper (TPS, percentiles, progress windows)
  runners/sysbench.py          sysbench oltp wrapper (TPS, tail latency)
  scenarios/__init__.py        registry: declared capabilities, ordering, central gating
  scenarios/*.py               one module per scenario
  charts.py                    figure building + static SVG rendering (matplotlib)
  report.py                    self-contained HTML report
  cli.py                       run | chart | report | check
tests/                         unit tests (no network, no live databases)
```

Your own config, charts, raw tool output and write-ups are local artifacts, not part of
the harness: `capybench.toml`, `charts/`, `raw/`, `report.html` and anything under
`internal/` are gitignored. Keep platform-specific results there and publish them through
a results PR (see [CONTRIBUTING.md](CONTRIBUTING.md)) rather than in the tracked tree.

## Adding a provider

The eight DSN-only scenarios need nothing: add a `[targets.<name>]` block with the DSN and
tier metadata. To bring a platform into the lifecycle scenarios:

1. Subclass `Provider` in a new module under `src/capybench/providers/`, set its `type`,
   and list in `capabilities` only what the platform genuinely does - then implement the
   matching methods:
   - `Capability.PROVISION` → `provision(name, *, pg_version, region) -> Target` and
     `destroy(target)`.
   - `Capability.BRANCH` → `create_branch(parent, branch_name) -> Target` and
     `delete_branch(parent, branch_name)`.
   - `Capability.SLEEP` → `trigger_sleep(target)`. There is no wake hook, because the next
     client connection is the wake and timing it is the measurement.
   - `Capability.RESTORE` → `restore(target, name, *, restore_time) -> Target`, which must
     restore into a *fresh* database and never over the source.
2. Read credentials from an environment variable (configurable via a `token_env`
   setting), never from the TOML.
3. Register the class in `src/capybench/providers/__init__.py` and add unit tests for
   its selection and settings parsing.
4. Document its settings in `capybench.toml.example` and add a row to the provider
   matrix above.

You never write a capability check: the runner reads your `capabilities` set and skips
what you do not implement.

Adapters must be complete to merge - a provider that claims a capability it cannot
deliver corrupts published comparisons.

## Adding a host probe

To publish host-vantage ground truth for a platform the `ssh` probe cannot reach,
subclass `HostProbe` in `src/capybench/hostprobe/`, implement `facts()` and `sample()`,
mark monotonic counters with `Reading(counter=True)` so the runner reports deltas, and
register it. Keep the parsing in pure functions and unit-test them against real output -
a probe that silently reports zeros is worse than no probe.

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 CapyDB.
