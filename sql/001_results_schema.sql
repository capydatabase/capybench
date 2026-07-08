-- CapyBench results schema.
--
-- Long/tall format: one row per (run, scenario, target, variant, metric). This keeps
-- heterogeneous scenarios (TPS vs branch-create-ms vs cold-start-ms) in one table and
-- makes charting a matter of filtering by `scenario` + `metric`.
--
-- Apply against a dedicated results database (dogfood a CapyDB project for this):
--   psql "$CAPYBENCH_RESULTS_DSN" -f sql/001_results_schema.sql

create extension if not exists pgcrypto;

create table if not exists bench_run (
    id               uuid primary key default gen_random_uuid(),
    harness_version  text        not null,
    git_sha          text,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    notes            text
);

comment on table bench_run is 'One row per harness invocation (a full or partial matrix run).';

create table if not exists bench_sample (
    id              bigint generated always as identity primary key,
    run_id          uuid        not null references bench_run (id) on delete cascade,
    scenario        text        not null,   -- throughput | noisy_neighbor | branch_speed | cold_start
    target          text        not null,   -- logical target name, e.g. capydb-small, neon-launch
    target_tier_usd numeric(10, 2),         -- monthly price of the compared tier (fairness axis)
    pg_version      text,
    region          text,
    dataset_bytes   bigint,                 -- parent/working-set size where meaningful
    concurrency     integer,                -- client count where meaningful
    variant         text,                   -- e.g. victim-alone | victim-under-attack | read-only
    metric          text        not null,   -- tps | latency_avg_ms | p95_latency_ms | p99_latency_ms |
                                            --   branch_create_ms | cold_start_ms | ...
    value           double precision not null,
    unit            text        not null,   -- tps | ms | bytes | s
    params          jsonb       not null default '{}'::jsonb,  -- scenario inputs (scale, duration, ...)
    raw             jsonb       not null default '{}'::jsonb,   -- parsed tool output for provenance
    created_at      timestamptz not null default now()
);

comment on table bench_sample is 'One measurement. Filter by (scenario, metric) to build a chart series.';

create index if not exists bench_sample_run_idx      on bench_sample (run_id);
create index if not exists bench_sample_scenario_idx on bench_sample (scenario, target);
create index if not exists bench_sample_metric_idx   on bench_sample (scenario, metric);
