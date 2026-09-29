# Changelog

All notable changes to `capybench` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

Load runs must originate from a client colocated with the target region to be meaningful.

## [Unreleased]

### Added

- `kv_latency` scenario: PING / SET / GET round trips on a warm connection, plus fresh
  connects, against a key-value store over both protocols it speaks - the Upstash-compatible
  REST API (`POST <rest_url>`, bearer token, kept-alive HTTPS) and RESP over TLS routed by SNI
  (`AUTH <token>`). Every variant (`rest-ping`, `resp-get`, `resp-fresh-connect`, ...) records
  min/avg/p50/p95/p99/max like `query_latency`, with a chart and a report section. Gated on the
  new `kv` capability: the `capydb` provider resolves the endpoints from
  `/v1/projects/{id}/kv/credentials`; the store must already exist and its token comes from
  the variable named by `token_env` (default `CAPYKV_REST_TOKEN`), since the API returns it
  only at creation or rotation. Targets on other providers skip with the usual message. Both
  clients are standard library, so no new dependency.

- CI (`.github/workflows/ci.yml`): `ruff check`, `ruff format --check` and `pytest` on every push
  and pull request to `main`. Until now only commit messages were checked.
- Open-loop load generation (`open_loop_rates_tps`, `open_loop_clients`,
  `open_loop_latency_limit_ms` under `[throughput]`). The existing concurrency sweep is closed
  loop, which is subject to coordinated omission: when the database stalls the client stalls with
  it, so the work that would have queued is never issued and never appears in the tail. An
  open-loop pass issues at a fixed arrival rate regardless, and reports
  `pgbench_queued_p50/p95/p99_ms` - service time plus queue wait, i.e. what a caller actually
  experienced - alongside `pgbench_schedule_lag_avg_ms` and `transactions_skipped`.

### Changed

- `parse_transaction_logs` returns `(latencies_ms, lags_ms)` and parses pgbench's seventh
  `schedule_lag` column. The two lists are kept index-aligned; a line with a missing lag is
  dropped rather than shifting every later pairing.
- README methodology gains a coordinated-omission entry, and notes that CapyDB's own terms
  explicitly permit benchmarking and publication.

- README, example configuration, CLI, config loader and result store revised for the
  provider-neutral scenario set.

### Fixed

- **A failed scenario could be reported as executed.** `_host_window` returned from its
  `finally` block when the closing host-probe sample failed, which discards the exception in
  flight - so a scenario that raised was logged as executed instead of FAILED. Python 3.14 also
  warns about it on every start (`SyntaxWarning: 'return' in a 'finally' block`, PEP 765).
- **`capybench report` crashed on every run** with `IndexError: tuple index out of range`: the
  run metadata query read an `attestation` column it never selected.

## [1.1.0] - 2026-08-17

### Added

- Reworked into a provider-neutral OSS harness. Targets bind to providers (`[providers.<name>]`,
  types `capydb` and `generic`); branch and cold-start scenarios skip gracefully against providers
  without those capabilities.
- `query_latency` scenario (warm p95 of 0.81 ms measured on production nodes).
- `capybench report` — a self-contained HTML report.
- MIT licence and contribution guide, ready to publish as `github.com/capydatabase/capybench`.

### Fixed

- **`pgbench -d` was being read as "database", not "debug".** Every throughput number produced
  before this fix is invalid. Re-measured fair-share under a noisy neighbour: −51% throughput,
  ×2.55 latency.

## [2026-07-08]

### Added

- First release: throughput, noisy-neighbour, branch-speed and cold-start scenarios, plus a sleep
  helper for measuring resume latency.
