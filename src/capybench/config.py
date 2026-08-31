"""Configuration model for CapyBench.

The whole suite is driven by one TOML file (see ``capybench.toml.example``). It declares:

* the **results** database (where samples land),
* one or more **targets** (a connectable Postgres, tagged with the price tier / region /
  version it represents so charts can compare like-for-like),
* per-scenario knobs (scale factors, durations, dataset sizes),
* **providers** - named adapter configurations (``[providers.<name>]``) that give the
  harness lifecycle control (provision, branch, forced sleep, restore) over the platform
  behind a target. Targets reference a provider block by name via their ``provider`` key.
  Targets that are just a plain DSN use the built-in ``generic`` provider (the default);
  scenarios needing a capability it lacks skip with an explanatory message.
* an optional **host probe** (``[host_probe]``) - the privileged vantage point, for an
  operator benchmarking their own infrastructure,
* an optional **attestation** (``[attestation]``) - ground truth the harness cannot
  verify, declared by whoever runs it and always published as claimed, not measured.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.conninfo import make_conninfo


@dataclass(frozen=True, slots=True)
class Target:
    """A connectable Postgres that represents a provider/tier under test."""

    name: str
    host: str
    port: int = 5432
    user: str = "postgres"
    dbname: str = "postgres"
    password: str | None = None
    sslmode: str = "prefer"
    # CA bundle for verify-ca/verify-full ("system" = the OS trust store).
    sslrootcert: str | None = None
    # Comparison metadata - the axes charts group by.
    tier_usd: float | None = None
    pg_version: str | None = None
    region: str | None = None
    # Provider key selects which [providers.<name>] block drives lifecycle ops.
    provider: str = "generic"
    # Provider-scoped identifier (a project/database ID) used by lifecycle ops.
    project: str | None = None

    def dsn(self) -> str:
        """libpq connection string for psycopg."""
        values: dict[str, str | int] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": self.dbname,
            "sslmode": self.sslmode,
        }
        if self.password is not None:
            values["password"] = self.password
        if self.sslrootcert is not None:
            values["sslrootcert"] = self.sslrootcert
        # libpq keyword DSNs require quoting/escaping spaces, quotes and
        # backslashes. make_conninfo prevents a credential from becoming a new
        # connection parameter (for example, changing sslmode).
        return make_conninfo(**values)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One ``[providers.<name>]`` block: which adapter implements it, plus its settings.

    ``type`` selects the adapter implementation (see ``capybench.providers``); it defaults
    to the block name, so a block named after a registered adapter needs no explicit
    ``type``. Every other key in the block is passed through to the adapter as a setting.
    """

    name: str
    type: str
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ThroughputConfig:
    scale: int = 50  # pgbench -s / rows generated
    duration_s: int = 60  # per data-point run length
    concurrencies: tuple[int, ...] = (1, 8, 16, 32, 64)
    sysbench_percentile: int = 99
    read_only: bool = False  # also run a read-only variant when True
    # Open-loop arrival rates (transactions/sec) to additionally measure at. Every
    # closed-loop number above is subject to coordinated omission: when the database
    # stalls, the fixed-concurrency client stalls with it, so the requests that would
    # have queued are never issued and never appear in the tail. An open-loop run
    # issues on a schedule regardless, which is how production load behaves.
    #
    # Pick rates that bracket the closed-loop result - one comfortably below the
    # measured peak TPS and one near it - so the pair shows both the healthy case and
    # the one where the queue starts forming. Empty = closed-loop only.
    open_loop_rates_tps: tuple[float, ...] = ()
    # Open-loop client count. Must be high enough that clients are not themselves the
    # limit at the requested rate; it is a ceiling on concurrency, not a target.
    open_loop_clients: int = 64
    # Abandon a transaction that has fallen this far behind schedule, the way a real
    # client with a timeout would. Keeps a saturated run from producing percentiles
    # dominated by an ever-growing backlog. None = never abandon.
    open_loop_latency_limit_ms: float | None = 10_000.0


@dataclass(frozen=True, slots=True)
class NoisyNeighborConfig:
    victim: str  # target name for the well-behaved workload
    aggressor: str  # target name (ideally same host) generating the storm
    victim_concurrency: int = 16
    aggressor_concurrency: int = 128
    duration_s: int = 60
    sysbench_percentile: int = 99


@dataclass(frozen=True, slots=True)
class BranchSpeedConfig:
    target: str  # parent target name
    parent_sizes_mb: tuple[int, ...] = (1024, 10240, 102400)
    repeats: int = 3  # branch creations per size for a distribution


@dataclass(frozen=True, slots=True)
class ColdStartConfig:
    target: str
    repeats: int = 30
    settle_s: int = 5  # wait after issuing sleep before the wake connect


@dataclass(frozen=True, slots=True)
class QueryLatencyConfig:
    targets: tuple[str, ...] | None = None  # None = every configured target
    repeats: int = 500  # warm queries per variant
    connect_repeats: int = 30  # fresh TLS+auth connects
    rows: int = 10_000  # seeded point-select table size


@dataclass(frozen=True, slots=True)
class FactSheetConfig:
    targets: tuple[str, ...] | None = None  # None = every configured target
    probe_gucs: bool = True  # try SET LOCAL on runtime GUCs to prove what is settable


@dataclass(frozen=True, slots=True)
class ConnectionCeilingConfig:
    targets: tuple[str, ...] | None = None
    # Hard cap on how many connections to open. Kept modest by default: this scenario
    # deliberately walks a database towards its limit.
    max_probe: int = 100
    connect_timeout_s: int = 10


@dataclass(frozen=True, slots=True)
class SustainedConfig:
    targets: tuple[str, ...] | None = None
    scale: int = 50
    concurrency: int = 16
    duration_s: int = 1800  # long enough to exhaust burst credits (30 min)
    window_s: int = 60  # pgbench --progress interval; one sample per window
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class VacuumUnderWriteConfig:
    target: str  # single target: this scenario drives sustained write load
    scale: int = 50
    concurrency: int = 8
    duration_s: int = 600
    sample_interval_s: int = 30


@dataclass(frozen=True, slots=True)
class ColdCacheConfig:
    targets: tuple[str, ...] | None = None
    # Working set per table. Two tables this size are created, so the pair evicts each
    # other from shared_buffers; size well above the target's shared_buffers to mean it.
    size_mb: int = 512
    repeats: int = 3


@dataclass(frozen=True, slots=True)
class ProvisionSpeedConfig:
    provider: str  # [providers.<name>] block - there is no target to create one from yet
    repeats: int = 3
    pg_version: str | None = None
    region: str | None = None
    tier_usd: float | None = None  # price tag for the created database, for fair charts


@dataclass(frozen=True, slots=True)
class RestoreRTOConfig:
    target: str
    repeats: int = 1
    restore_time: str | None = None  # RFC 3339; None = most recent recoverable state


@dataclass(frozen=True, slots=True)
class HostProbeConfig:
    """The ``[host_probe]`` block: privileged access to the node under test."""

    name: str
    type: str
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Suite:
    results_dsn: str
    targets: dict[str, Target]
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    throughput: ThroughputConfig | None = None
    noisy_neighbor: NoisyNeighborConfig | None = None
    branch_speed: BranchSpeedConfig | None = None
    cold_start: ColdStartConfig | None = None
    query_latency: QueryLatencyConfig | None = None
    fact_sheet: FactSheetConfig | None = None
    connection_ceiling: ConnectionCeilingConfig | None = None
    sustained: SustainedConfig | None = None
    vacuum_under_write: VacuumUnderWriteConfig | None = None
    cold_cache: ColdCacheConfig | None = None
    provision_speed: ProvisionSpeedConfig | None = None
    restore_rto: RestoreRTOConfig | None = None
    host_probe: HostProbeConfig | None = None
    # Operator-declared ground truth the harness cannot verify (node spec, colocation,
    # tenant density). Published as claimed, never as measured.
    attestation: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    # Where the client machine running the harness lives (e.g. "eu-central-1a", "hel1").
    # Recorded with every run; published results should always state it.
    client_region: str | None = None
    # Number of times `capybench run` executes the selected scenarios within one run.
    # A single pass on a node shared with live workloads is not publishable: throughput
    # moved by tens of percent between identical runs purely on how many neighbouring
    # cells happened to be awake. >=5 and report the distribution.
    run_repeats: int = 1
    # The exact client image/instance this suite is meant to run from, e.g.
    # "hetzner:cx43:ubuntu-26.04". Recorded on every run. Fresh-connect is dominated by
    # client-side TLS+SCRAM cost, so numbers from different client images are not
    # comparable no matter how stable the server is.
    client_image: str | None = None

    def target(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.targets)) or "(none)"
            raise KeyError(f"target {name!r} not defined; known targets: {known}") from exc

    def provider_config(self, target: Target) -> ProviderConfig:
        """The ``[providers.<name>]`` block a target references.

        A missing block is synthesized as ``type = <name>`` with no settings, so built-in
        zero-config adapters (``generic``) need no TOML block at all.
        """
        cfg = self.providers.get(target.provider)
        if cfg is not None:
            return cfg
        return ProviderConfig(name=target.provider, type=target.provider)


def _target_from_toml(name: str, raw: dict[str, object]) -> Target:
    def _req_str(key: str, default: str | None = None) -> str:
        val = raw.get(key, default)
        if not isinstance(val, str):
            raise TypeError(f"target {name!r}: {key!r} must be a string")
        return val

    def _opt_float(key: str) -> float | None:
        val = raw.get(key)
        if val is None:
            return None
        if not isinstance(val, (int, float)):
            raise TypeError(f"target {name!r}: {key!r} must be a number")
        return float(val)

    port = raw.get("port", 5432)
    if not isinstance(port, int):
        raise TypeError(f"target {name!r}: 'port' must be an integer")

    password = raw.get("password")
    if password is not None and not isinstance(password, str):
        raise TypeError(f"target {name!r}: 'password' must be a string")

    project = raw.get("project")
    if project is not None and not isinstance(project, str):
        raise TypeError(f"target {name!r}: 'project' must be a string")

    return Target(
        name=name,
        host=_req_str("host"),
        port=port,
        user=_req_str("user", "postgres"),
        dbname=_req_str("dbname", "postgres"),
        password=password,
        sslmode=_req_str("sslmode", "prefer"),
        sslrootcert=raw.get("sslrootcert") if isinstance(raw.get("sslrootcert"), str) else None,
        tier_usd=_opt_float("tier_usd"),
        pg_version=raw.get("pg_version") if isinstance(raw.get("pg_version"), str) else None,
        region=raw.get("region") if isinstance(raw.get("region"), str) else None,
        provider=_req_str("provider", "generic"),
        project=project,
    )


def _providers_from_toml(data: dict[str, object]) -> dict[str, ProviderConfig]:
    raw_providers = data.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raise TypeError("[providers] must be a table of [providers.<name>] blocks")

    providers: dict[str, ProviderConfig] = {}
    for name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            raise TypeError(f"[providers.{name}] must be a table")
        ptype = raw.get("type", name)
        if not isinstance(ptype, str):
            raise TypeError(f"[providers.{name}]: 'type' must be a string")
        settings = {key: val for key, val in raw.items() if key != "type"}
        providers[name] = ProviderConfig(name=name, type=ptype, settings=settings)
    return providers


def _host_probe_from_toml(data: dict[str, object]) -> HostProbeConfig | None:
    raw = data.get("host_probe")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("[host_probe] must be a table")
    ptype = raw.get("type", "ssh")
    if not isinstance(ptype, str):
        raise TypeError("[host_probe]: 'type' must be a string")
    name = raw.get("name", ptype)
    if not isinstance(name, str):
        raise TypeError("[host_probe]: 'name' must be a string")
    settings = {key: val for key, val in raw.items() if key not in ("type", "name")}
    return HostProbeConfig(name=name, type=ptype, settings=settings)


def _attestation_from_toml(data: dict[str, object]) -> dict[str, str]:
    raw = data.get("attestation")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError("[attestation] must be a table of scalar values")
    out: dict[str, str] = {}
    for key, val in raw.items():
        if isinstance(val, (dict, list)):
            raise TypeError(f"[attestation] {key!r} must be a scalar, not a table or array")
        out[key] = str(val)
    return out


def _opt_target_list(section: dict[str, object], label: str) -> tuple[str, ...] | None:
    """Parse an optional ``targets = [...]`` key (absent/empty = every target)."""
    raw = section.get("targets")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise TypeError(f"[{label}] 'targets' must be a list of strings")
    return tuple(raw) or None


def _req_str_key(section: dict[str, object], key: str, label: str) -> str:
    val = section.get(key)
    if not isinstance(val, str) or not val:
        raise TypeError(f"[{label}] requires {key!r} to be a non-empty string")
    return val


def _opt_str_key(section: dict[str, object], key: str, label: str) -> str | None:
    val = section.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise TypeError(f"[{label}] {key!r} must be a string")
    return val


def load(path: str | Path) -> Suite:
    """Parse and validate a suite TOML file."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))

    results = data.get("results")
    if not isinstance(results, dict) or not isinstance(results.get("dsn"), str):
        raise ValueError("config must define [results] with a string 'dsn'")

    targets_raw = data.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ValueError("config must define at least one [targets.<name>] block")
    targets = {
        name: _target_from_toml(name, raw)
        for name, raw in targets_raw.items()
        if isinstance(raw, dict)
    }

    def _tuple_ints(
        section: dict[str, object], key: str, default: tuple[int, ...]
    ) -> tuple[int, ...]:
        val = section.get(key, list(default))
        if not isinstance(val, list) or not all(isinstance(v, int) for v in val):
            raise TypeError(f"{key!r} must be a list of integers")
        return tuple(int(v) for v in val)

    throughput = None
    if isinstance(tp := data.get("throughput"), dict):
        throughput = ThroughputConfig(
            scale=int(tp.get("scale", 50)),
            duration_s=int(tp.get("duration_s", 60)),
            concurrencies=_tuple_ints(tp, "concurrencies", (1, 8, 16, 32, 64)),
            sysbench_percentile=int(tp.get("sysbench_percentile", 99)),
            read_only=bool(tp.get("read_only", False)),
        )

    noisy = None
    if isinstance(nn := data.get("noisy_neighbor"), dict):
        noisy = NoisyNeighborConfig(
            victim=str(nn["victim"]),
            aggressor=str(nn["aggressor"]),
            victim_concurrency=int(nn.get("victim_concurrency", 16)),
            aggressor_concurrency=int(nn.get("aggressor_concurrency", 128)),
            duration_s=int(nn.get("duration_s", 60)),
            sysbench_percentile=int(nn.get("sysbench_percentile", 99)),
        )

    branch = None
    if isinstance(bs := data.get("branch_speed"), dict):
        branch = BranchSpeedConfig(
            target=str(bs["target"]),
            parent_sizes_mb=_tuple_ints(bs, "parent_sizes_mb", (1024, 10240, 102400)),
            repeats=int(bs.get("repeats", 3)),
        )

    cold = None
    if isinstance(cs := data.get("cold_start"), dict):
        cold = ColdStartConfig(
            target=str(cs["target"]),
            repeats=int(cs.get("repeats", 30)),
            settle_s=int(cs.get("settle_s", 5)),
        )

    query_latency = None
    if isinstance(ql := data.get("query_latency"), dict):
        query_latency = QueryLatencyConfig(
            targets=_opt_target_list(ql, "query_latency"),
            repeats=int(ql.get("repeats", 500)),
            connect_repeats=int(ql.get("connect_repeats", 30)),
            rows=int(ql.get("rows", 10_000)),
        )

    fact_sheet = None
    if isinstance(fs := data.get("fact_sheet"), dict):
        fact_sheet = FactSheetConfig(
            targets=_opt_target_list(fs, "fact_sheet"),
            probe_gucs=bool(fs.get("probe_gucs", True)),
        )

    connection_ceiling = None
    if isinstance(cc := data.get("connection_ceiling"), dict):
        connection_ceiling = ConnectionCeilingConfig(
            targets=_opt_target_list(cc, "connection_ceiling"),
            max_probe=int(cc.get("max_probe", 100)),
            connect_timeout_s=int(cc.get("connect_timeout_s", 10)),
        )

    sustained = None
    if isinstance(su := data.get("sustained"), dict):
        sustained = SustainedConfig(
            targets=_opt_target_list(su, "sustained"),
            scale=int(su.get("scale", 50)),
            concurrency=int(su.get("concurrency", 16)),
            duration_s=int(su.get("duration_s", 1800)),
            window_s=int(su.get("window_s", 60)),
            read_only=bool(su.get("read_only", False)),
        )

    vacuum = None
    if isinstance(vw := data.get("vacuum_under_write"), dict):
        vacuum = VacuumUnderWriteConfig(
            target=_req_str_key(vw, "target", "vacuum_under_write"),
            scale=int(vw.get("scale", 50)),
            concurrency=int(vw.get("concurrency", 8)),
            duration_s=int(vw.get("duration_s", 600)),
            sample_interval_s=int(vw.get("sample_interval_s", 30)),
        )

    cold_cache = None
    if isinstance(cch := data.get("cold_cache"), dict):
        cold_cache = ColdCacheConfig(
            targets=_opt_target_list(cch, "cold_cache"),
            size_mb=int(cch.get("size_mb", 512)),
            repeats=int(cch.get("repeats", 3)),
        )

    provision_speed = None
    if isinstance(ps := data.get("provision_speed"), dict):
        tier = ps.get("tier_usd")
        if tier is not None and (isinstance(tier, bool) or not isinstance(tier, (int, float))):
            raise TypeError("[provision_speed] 'tier_usd' must be a number")
        provision_speed = ProvisionSpeedConfig(
            provider=_req_str_key(ps, "provider", "provision_speed"),
            repeats=int(ps.get("repeats", 3)),
            pg_version=_opt_str_key(ps, "pg_version", "provision_speed"),
            region=_opt_str_key(ps, "region", "provision_speed"),
            tier_usd=float(tier) if tier is not None else None,
        )

    restore_rto = None
    if isinstance(rr := data.get("restore_rto"), dict):
        restore_rto = RestoreRTOConfig(
            target=_req_str_key(rr, "target", "restore_rto"),
            repeats=int(rr.get("repeats", 1)),
            restore_time=_opt_str_key(rr, "restore_time", "restore_rto"),
        )

    client_region = data.get("client_region")
    if client_region is not None and not isinstance(client_region, str):
        raise TypeError("'client_region' must be a string")

    return Suite(
        results_dsn=results["dsn"],
        targets=targets,
        providers=_providers_from_toml(data),
        throughput=throughput,
        noisy_neighbor=noisy,
        branch_speed=branch,
        cold_start=cold,
        query_latency=query_latency,
        fact_sheet=fact_sheet,
        connection_ceiling=connection_ceiling,
        sustained=sustained,
        vacuum_under_write=vacuum,
        cold_cache=cold_cache,
        provision_speed=provision_speed,
        restore_rto=restore_rto,
        host_probe=_host_probe_from_toml(data),
        attestation=_attestation_from_toml(data),
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
        client_region=client_region,
        run_repeats=_positive_int(data.get("run_repeats"), "run_repeats", default=1),
        client_image=(
            data.get("client_image") if isinstance(data.get("client_image"), str) else None
        ),
    )


def _positive_int(value: object, name: str, *, default: int) -> int:
    """Config ints that must be >= 1; None means "use the default"."""
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TypeError(f"'{name}' must be an integer >= 1")
    return value
