-- Vantage + facts + attestation.
--
-- Three additions that make results self-describing:
--   * bench_sample.vantage  - was this measured through a client DSN, or with privileged
--                             access on the platform's own node? The two answer different
--                             questions and must not share a chart axis.
--   * bench_run.attestation - ground truth the harness cannot verify (node spec, whether
--                             victim and aggressor really are colocated, tenant density),
--                             declared by whoever ran it and always rendered as claimed.
--   * bench_fact            - non-numeric observations (settings, versions, capability
--                             verdicts). A TPS number means nothing without the durability
--                             posture that produced it.
--
-- Fresh installs get all of this from 001; this migration upgrades existing result DBs.
--
--   psql "$CAPYBENCH_RESULTS_DSN" -f sql/003_facts_and_vantage.sql

alter table bench_run
    add column if not exists attestation jsonb not null default '{}'::jsonb;

comment on column bench_run.attestation is
    'Facts the harness cannot verify, declared by whoever ran it (node spec, colocation, tenant density). Always rendered as claimed, never as measured.';

alter table bench_sample
    add column if not exists vantage text not null default 'client';

comment on column bench_sample.vantage is
    'client = measured through a normal DSN (any customer can reproduce it); host = measured with privileged access on the platform node. Never mix vantages on one chart axis.';

create table if not exists bench_fact (
    id         bigint generated always as identity primary key,
    run_id     uuid        not null references bench_run (id) on delete cascade,
    scenario   text        not null,
    target     text        not null,
    vantage    text        not null default 'client',
    category   text,
    key        text        not null,
    value      text        not null,
    source     text        not null,
    created_at timestamptz not null default now()
);

comment on table bench_fact is 'One non-numeric observation about a target. source=probe means the behavior was measured, not read from a setting.';

create unique index if not exists bench_fact_unique_idx on bench_fact (run_id, target, vantage, key);
create index if not exists bench_fact_run_idx on bench_fact (run_id, category);
