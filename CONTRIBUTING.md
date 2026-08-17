# Contributing to CapyBench

Thanks for helping build a credible, provider-neutral benchmark for managed Postgres.
Two kinds of contributions matter most: **provider adapters** and **reproducible
results**.

## Development setup

```bash
uv sync
uv run ruff check          # lint (must be clean)
uv run ruff format --check # formatting (must be clean)
uv run pytest              # unit tests (no network, no live DBs)
```

Python 3.13+, managed with [uv](https://docs.astral.sh/uv/). Keep `uv.lock` committed -
reproducibility applies to the harness itself, too.

## Two vantage points, and why they never mix

Every recorded row carries a `vantage`:

- `client` - measured through a normal DSN. Anyone can reproduce it. This is what gets
  published and charted.
- `host` - measured with privileged access on the node (cgroup throttling, PSI, cache hit
  ratios). Only an operator can produce it; it corroborates client numbers.

Chart queries filter `vantage = 'client'`. If you add a chart, keep that filter: a reader
seeing a chart assumes they could reproduce it, and mixing in privileged readings quietly
breaks that promise. Host readings belong in the report's host section.

The same principle governs `[attestation]`: anything the harness cannot verify (node
hardware, whether two targets share a machine) is rendered as *declared*, never measured.
Do not add code that treats an attested value as a measurement.

## Adding a provider adapter

The adapter interface lives in `src/capybench/providers/base.py` and is intentionally
small - it is derived from what the scenarios actually call. Each operation is gated by a
`Capability` the adapter advertises:

| Capability | Methods | Scenario |
|---|---|---|
| `PROVISION` | `provision(name, *, pg_version, region)`, `destroy(target)` | `provision_speed` |
| `BRANCH` | `create_branch(parent, name)`, `delete_branch(parent, name)` | `branch_speed` |
| `SLEEP` | `trigger_sleep(target)` (no wake hook: the first client connection after sleep is the wake, and timing it is the measurement) | `cold_start` |
| `RESTORE` | `restore(target, name, *, restore_time)` | `restore_rto` |

Ground rules:

1. **Complete or absent.** Only list a capability if the implementation genuinely works
   end-to-end against the real platform. We do not merge stubs, mocks, or half-wired
   adapters - they corrupt published comparisons. `generic` already covers the DSN-only
   scenarios for every Postgres, which is most of the suite.
2. **Never destructive.** `restore` must restore into a *fresh* database, never over the
   source; `provision` must pair with a `destroy` that actually releases the resource.
   A benchmark that can eat production is one nobody runs twice.
3. **Secrets from the environment.** Tokens come from an env var (expose a `token_env`
   setting); the TOML must stay committable.
4. **Fail loudly.** Wrap platform errors in `ProviderError` with enough context to debug
   a failed run. No silent fallbacks.
5. **Register and test.** Add the class to `_REGISTRY` in
   `src/capybench/providers/__init__.py`, unit-test selection/settings parsing in
   `tests/`, document settings in `capybench.toml.example`, and update the provider
   matrix in the README.
6. State in the PR description which platform account/plan you validated against and
   include a (secret-free) sample config.

Do not write capability checks inside a provider or a scenario - the registry does it once
and produces the same message everywhere.

## Adding a scenario

Scenarios live in `src/capybench/scenarios/` and expose `run(suite, run, *, log)`.

- **Register it** in `SPECS` in `scenarios/__init__.py` with the capabilities it needs
  (usually none) and, if it needs one, which config field names the target or provider.
  Add a matching config dataclass and parser in `config.py`; the name of the `Suite` field
  must equal the scenario name.
- **Place it in the order deliberately.** Read-only observation first, sustained load
  after, anything that disturbs the target last. A scenario that sleeps or restores a
  target skews everything that runs after it.
- Derive any new provider hook from a concrete scenario need - do not add speculative
  interface methods. If no provider can implement it, the capability should not exist.
- Record measurements through `store.Sample` with honest units, tagged with
  `tier_usd` / `pg_version` / `region` so charts stay like-for-like. Record non-numeric
  observations through `store.Fact` - do not coerce a setting into a float.
- **Put the interpretation in a pure function** (see `sustained.drift`,
  `fact_sheet.durability_posture`) and unit-test it. The step from numbers to a claim is
  where a benchmark misleads people, so it must be testable without a database.
- **Make the measurement check itself where it can.** `cold_cache` reports the buffer hit
  ratio it achieved so a failed eviction is visible rather than reported as a fast disk;
  `vacuum_under_write` fails loudly if its write load never ran. Prefer a scenario that
  can prove it measured what it claims.
- Add a chart builder in `charts.py` (it feeds both `chart` and `report`) and document the
  scenario's methodology in the README, including its honest limits.

## Adding a host probe

`src/capybench/hostprobe/`. Implement `facts()` and `sample()`, mark monotonic counters
with `Reading(counter=True)` so the runner reports deltas rather than nonsense, and keep
all parsing in pure functions unit-tested against real output. A probe that silently
returns zeros is worse than no probe - raise `HostProbeError` instead.

## Submitting results

Results PRs are welcome. To be comparable, a result must be reproducible; include:

1. **The exact TOML used** (secret-free - no passwords, no tokens).
2. **The `fact_sheet` output.** Numbers without the durability and resource posture that
   produced them are not comparable, and we will ask for it. Run the scenario; the report
   includes it automatically.
3. **Client machine spec and region.** The client must be colocated (same region /
   datacenter) with the targets; state both explicitly. Cross-region results will not
   be accepted for comparison tables.
4. **The generated `report.html`** (it embeds run metadata, config echo, facts, and charts).
5. Provider plan/tier and monthly price for every target (`tier_usd`), PG versions, and
   the run date.
6. For `noisy_neighbor`, an `[attestation]` stating that the two targets share a physical
   node. The harness cannot verify it, and the result means nothing without it.
7. Anything unusual about the environment (burstable instances, cold caches, quota
   limits hit, etc.).

Before naming a vendor in published numbers, check that vendor's terms of service -
some restrict benchmark publication. When in doubt, submit the result category-labeled
(e.g. "shared-tenancy managed PG") rather than vendor-named.

## Code style

- `ruff check` and `ruff format` are the only formatters/linters; both must pass.
- No `type: ignore` without a justification comment; no bare `except`; no swallowed
  errors.
- Keep modules single-purpose and the provider interface minimal.
