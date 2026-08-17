-- Add reproducibility metadata to bench_run: which machine (and region) ran the harness.
-- Fresh installs get this column from 001; this migration upgrades existing result DBs.
--
--   psql "$CAPYBENCH_RESULTS_DSN" -f sql/002_run_client.sql

alter table bench_run
    add column if not exists client jsonb not null default '{}'::jsonb;

comment on column bench_run.client is
    'Client machine info recorded at run start (hostname, platform, cpu_count, region).';
