-- CapyBench results schema.
--
-- Long/tall format: one row per (run, scenario, target, variant, metric). This keeps
-- heterogeneous scenarios (TPS vs branch-create-ms vs cold-start-ms) in one table and
-- makes charting a matter of filtering by `scenario` + `metric`.
--
-- Apply against a dedicated results database, in order with any later sql/NNN_*.sql:
--   psql "$CAPYBENCH_RESULTS_DSN" -f sql/001_results_schema.sql

create extension if not exists pgcrypto;

create table if not exists bench_run (
    id               uuid primary key default gen_random_uuid(),
    harness_version  text        not null,
    git_sha          text,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    notes            text,
    client           jsonb       not null default '{}'::jsonb,  -- machine/region reproducibility info
    attestation      jsonb       not null default '{}'::jsonb   -- operator-declared ground truth (unverified)
);

comment on table bench_run is 'One row per harness invocation (a full or partial matrix run).';
comment on column bench_run.attestation is
    'Facts the harness cannot verify, declared by whoever ran it (node spec, colocation, tenant density). Always rendered as claimed, never as measured.';

create table if not exists bench_sample (
    id              bigint generated always as identity primary key,
    run_id          uuid        not null references bench_run (id) on delete cascade,
    scenario        text        not null,   -- throughput | noisy_neighbor | sustained | cold_start | ...
    target          text        not null,   -- logical target name from [targets.<name>], e.g. pg-a
    vantage         text        not null default 'client',  -- client (customer-reproducible) | host (privileged)
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
comment on column bench_sample.vantage is
    'client = measured through a normal DSN (any customer can reproduce it); host = measured with privileged access on the platform node. Never mix vantages on one chart axis.';

create index if not exists bench_sample_run_idx      on bench_sample (run_id);
create index if not exists bench_sample_scenario_idx on bench_sample (scenario, target);
create index if not exists bench_sample_metric_idx   on bench_sample (scenario, metric);

-- Non-numeric observations: settings, versions, capability verdicts, host facts. These
-- are what make a number comparable (a throughput figure measured with synchronous_commit
-- off is not the same measurement as one taken durable), so they are first-class rows
-- rather than a jsonb blob hanging off a sample.
create table if not exists bench_fact (
    id         bigint generated always as identity primary key,
    run_id     uuid        not null references bench_run (id) on delete cascade,
    scenario   text        not null,
    target     text        not null,
    vantage    text        not null default 'client',
    category   text,                   -- durability | resources | extensions | roles | pooling | host | ...
    key        text        not null,   -- e.g. synchronous_commit, pgvector_available
    value      text        not null,   -- always text; numbers keep their source formatting
    source     text        not null,   -- pg_settings | probe | provider_api | host
    created_at timestamptz not null default now()
);

comment on table bench_fact is 'One non-numeric observation about a target. source=probe means the behavior was measured, not read from a setting.';

create unique index if not exists bench_fact_unique_idx on bench_fact (run_id, target, vantage, key);
create index if not exists bench_fact_run_idx on bench_fact (run_id, category);
