# CapyBench — first live run (capydb-db1, hel1)

**Date:** 2026-07-08 · **Store:** run `0c3aa5b6` · **Charts:** [`charts/`](charts/)

> **RE-BENCH 2026-07-08 (same day, after isolation hardening) — see [§Re-bench](#re-bench-2026-07-08-after-isolation-hardening) at the bottom.**
> Headline: noisy-neighbor victim impact improved from **−55 % tps / p99 ×3.3** to
> **−41 % / ×1.7** after per-instance `CPUWeight=plan` + `CPUQuota` clamped to
> (cores−1)×100 %; **E1 cold-start measured: p50 ≈ 250 ms, max 294 ms** (budget
> p50 < 1 s / p99 < 2 s) and **scale-to-zero was enabled in prod** the same day.

First real run of the benchmark harness against production **capydb-db1** (region `hel1`,
ccx23 — 4 dedicated vCPU / 16 GB, single file-backed ZFS pool `tank`). Two throwaway
projects (`capybench-victim`, `capybench-aggressor`) provisioned via the admin API; load
generated from a colocated throwaway Hetzner client in hel1.

> This is a **single-provider baseline + a self-audit of the isolation claim** — no
> competitors were measured yet. The headline is finding #2: **per-instance isolation did
> not hold under a saturating neighbor on this host.**

---

## TL;DR

| # | Scenario | Result | Reading |
|---|---|---|---|
| 1 | **Throughput** | 2 283 tps @ 32 clients (pgbench); p99 25→61 ms as clients rise | Healthy single-instance baseline for a dev-tier project on a shared 4-vCPU box. |
| 2 | **Noisy neighbor** | Victim **−55 % tps** (929→416) and **p99 ×3.3** (27.7→90.8 ms) under a co-located aggressor | ⚠️ **Isolation did not protect the victim.** Needs investigation before it's a marketing claim. |
| 3 | **Branch speed** | 256 MB→**11.8 s**, 1 GB→**14.2 s** (4× data → +20 % time) | ZFS CoW works: clone time tracks provisioning, not data size. |
| 4 | **Cold start** | **not run** | Scale-to-zero is disabled in prod (no wake/sleep API); needs the flag or agent access. |

---

## Why the client location mattered (methodology)

The first attempt ran the load from a **laptop → hel1**: ~**68 ms RTT**. sysbench's OLTP
transaction is ~20 query round-trips, so each transaction carried ~1.4 s of pure network —
the benchmark measured the internet, not the database (2.68 tps, 1475 ms "latency").

Re-running from a **throwaway Hetzner box in hel1** dropped RTT to **~0.5–1.4 ms**, which is
what produced the numbers below. **Latency-bound scenarios (throughput, noisy-neighbor) are
only valid from a client colocated with the DB.** branch-speed is immune (it measures
server-side provisioning, not per-query RTT), which is why it was valid from the laptop too.

**Environment**
- DB host: capydb-db1, `hel1`, 4 vCPU / 16 GB, single file-backed ZFS pool, PG 17.10.
- Instances: two dev-tier projects, `max_connections=60`, reached through the SNI routing
  proxy (`*.db.capydb.dev`, TLS `require`).
- Load client: Hetzner `cx23` (2 vCPU) in hel1, pgbench 16.14 + sysbench 1.0.20 (pgsql).
- The host was **oversubscribed** during the test: 5 real app DBs (epafi/fairrent/
  capysquash/Ergosphere) + 3 benchmark projects share the same 4 vCPUs and one pool.

---

## 1 — Throughput (single instance)

![throughput](charts/throughput.png)

| Clients | pgbench tps | pgbench avg (ms) | sysbench tps | sysbench p99 (ms) |
|--:|--:|--:|--:|--:|
| 1  | 154   | 6.5  | 74    | 24.8 |
| 8  | 1 357 | 5.9  | 603   | 23.5 |
| 16 | 1 876 | 8.5  | 873   | 46.6 |
| 32 | 2 283 | 14.0 | 1 120 | 61.1 |

TPS scales cleanly to 32 clients; the p99 knee at c≥16 is expected for a 4-vCPU host shared
with other tenants (the box is CPU-bound past ~8 concurrent heavy clients). This is a
**baseline**, not a competitive claim — the interesting comparison (same $/mo tier vs Neon/
Supabase/RDS) is future work.

## 2 — Noisy neighbor  ⚠️ the finding

![noisy neighbor](charts/noisy_neighbor.png)

Victim runs sysbench at 16 threads. Measured **alone**, then again while `capybench-aggressor`
(a *different* project/instance on the same host) runs pgbench at 48 clients, sustaining
~1 350–2 670 tps throughout the window.

| | Victim tps | Victim p99 (ms) |
|---|--:|--:|
| Alone | 929 | 27.7 |
| Under attack | 416 | 90.8 |
| **Δ** | **−55 %** | **+228 % (×3.3)** |

**This is the opposite of the isolation story.** On the current db1 config, a maxed-out
co-located neighbor materially degraded an unrelated tenant. Per-instance cgroups did **not**
contain it. Candidate causes to investigate before any "noisy-neighbor-proof" messaging:

- **CPU:** are per-instance `cpu.max` **hard quotas** set, or only `cpu.weight` (shares)?
  Shares don't cap a neighbor under contention — they only arbitrate it.
- **I/O:** is `io.max` configured per instance? A single shared file-backed ZFS pool with no
  per-cgroup I/O limits means disk contention is unmanaged.
- **Oversubscription:** 4 vCPU hosting 8 instances + the aggressor is worst-case. A
  correctly-sized host (fewer instances/vCPU, or the aggressor capped) may behave very
  differently — **re-test after the isolation config changes.**

> Caveat: this measures a *worst case* (dev-tier instances, oversubscribed dogfood host,
> neighbor at full tilt). It is a real signal that the isolation config needs hardening, not
> proof that isolation can never work here.

## 3 — Branch speed (ZFS copy-on-write)

![branch speed](charts/branch_speed.png)

Branch = preview-database in `mode=clone`. Time is request → `SELECT 1` connectable.

| Parent size | Clone time (avg of 3) |
|--:|--:|
| 256 MB | 11.8 s |
| 1 GB (4× data) | 14.2 s |

4× the data adds only ~20 %, confirming the CoW property: clone cost is dominated by
control-plane orchestration + new-postmaster boot, **not** copying data. A snapshot/restore
competitor scales ~linearly with size — that's the differentiator to chart against them.

Caveats: only 2 size points; the helper polls at **2 s granularity**, so absolute times are
quantized ±2 s (the trend is the signal, not the exact seconds). Extend to 10/50/100 GB and
tighten polling to sharpen the flat-line story.

## 4 — Cold start — not measured

Scale-to-zero is deliberately **off** in prod (feature flags unset); there is no public
wake/sleep API and the wake agent is loopback-only on db1. Measuring socket-activated wake
needs either the flag enabled on a test instance or direct agent access on the host.

---

## What this run is and isn't

- **Is:** a validated harness + a real single-instance baseline + a concrete isolation
  finding, all reproducible.
- **Isn't:** a competitor comparison (no Neon/Supabase/RDS yet), a multi-tier study, or a
  representative production sizing (db1 is an oversubscribed dogfood box).

## Next

1. **Fix/verify isolation** (cpu.max hard caps + io.max per instance), then re-run scenario 2
   — this is the one that changes the product story.
2. Add competitor targets at matching $/mo tiers (harness supports it: add `[targets.*]`).
3. Enable scale-to-zero on a test instance for cold-start numbers.
4. Extend branch-speed to larger parents; tighten poll granularity.

## Reproduce

```bash
cd capybench
uv sync && brew install sysbench
# results store (local ephemeral PG used here; or a reachable Postgres):
psql "$DSN" -f sql/001_results_schema.sql
# edit capybench.toml (targets, results.dsn), then from a client COLOCATED with the DB:
uv run capybench run --config capybench.toml --only throughput,noisy_neighbor
uv run capybench chart --config capybench.toml --out charts/
```

Live artifacts still up for the re-bench: projects `capybench-victim`/`-aggressor` on db1,
load client `capybench-client` (46.62.169.126, hel1). Raw tool output archived in the session
scratchpad (`box-output.txt`, `noisy-output.txt`).

---

## Re-bench 2026-07-08 (after isolation hardening)

Same host, same methodology (colocated hel1 client, identical sysbench/pgbench params;
raw output in [`raw/2026-07-08-rebench/`](raw/2026-07-08-rebench/)). Fresh business-plan
projects `capybench-victim`/`capybench-aggressor` (the originals were deleted; the org+slug
release needed a schema fix — migration 024 makes the slug unique index live-rows-only).

**What changed on the host between the runs:**
- Every `capydb-pg@` instance now gets `CPUWeight = plan CPU pct` (100/200/400) instead of
  a uniform 100, so contention is arbitrated proportionally to plan.
- `CPUQuota` is clamped to `(cores−1)×100 %` (300 % on the 4-vCPU db1), so a single tenant
  can no longer saturate the whole box (before: business plan = 400 % = all 4 cores).
- No io controls were added (cgroup io.max is ineffective on ZFS — I/O is issued by kernel
  z_* threads outside the tenant cgroup); CPU is the operative lever.

### Throughput (victim alone) — unchanged-to-better despite the 300 % clamp

| Clients | pgbench tps (was) | sysbench tps (was) | sysbench p99 ms (was) |
|--:|--:|--:|--:|
| 1  | 169 (154)     | 80 (74)       | 18.3 (24.8) |
| 8  | 1 391 (1 357) | 626 (603)     | 24.4 (23.5) |
| 16 | 2 162 (1 876) | 880 (873)     | 30.3 (46.6) |
| 32 | 2 591 (2 283) | 1 173 (1 120) | 47.5 (61.1) |

### Noisy neighbor — materially better, now fair-share (not full isolation)

| | tps | p99 (ms) |
|---|--:|--:|
| Victim alone | 775 | 36.2 |
| Under attack | 460 | 62.2 |
| **Δ (re-bench)** | **−41 %** | **×1.7** |
| Δ (first run) | −55 % | ×3.3 |

Both tenants are business-plan (equal CPUWeight=400, each clamped to 3 of 4 cores), so
under contention each gets ~fair share — the victim keeps ~60 % of its solo throughput
while the aggressor still sustains 1 500–3 500 tps. This is the intended shared-model
behavior: **proportional fairness with a saturation cap**, not noisy-neighbor-proof
isolation. Full isolation remains the premium/dedicated-tier story.

### E1 cold-start — measured, budget passed, scale-to-zero ENABLED in prod

12 sleep→wake cycles (admin force-sleep endpoint `POST /v1/admin/instances/{id}/sleep`,
then a timed `psql select 1` from the colocated client; the connect itself wakes the
instance through the routing proxy → per-host agent):

```
277 269 244 75 245 264 276 257 234 294 246 230   (ms)
```

**p50 ≈ 250 ms, max 294 ms** against the spec budget of p50 < 1 s / p99 < 2 s — passed
with ~4× headroom. `CAPYDB_SCALE_TO_ZERO_ENABLED=true` was set on the prod worker the
same day (idle window 5 m); the proxy wake path had just been hardened with in-proxy
single-flight (one agent wake per connection herd) and a hold-and-poll on the
durable-fallback path (a broken agent now costs latency, not the connection).

### Still open
- Re-run branch-speed at larger parents (unchanged from first run).
- Competitor targets at matching $/mo tiers.
- Cross-plan noisy-neighbor (vibe victim vs business aggressor) — weights now differ
  100 vs 400, so the vibe victim should degrade more; quantify before publishing
  fairness numbers.
