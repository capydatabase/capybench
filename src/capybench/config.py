"""Configuration model for CapyBench.

The whole suite is driven by one TOML file (see ``capybench.toml``). It declares:

* the **results** database (where samples land),
* one or more **targets** (a connectable Postgres, tagged with the price tier / region /
  version it represents so charts can compare like-for-like),
* per-scenario knobs (scale factors, durations, dataset sizes),
* **control** command templates — shell commands that drive a provider's lifecycle
  (branch create, sleep) so the harness never hardcodes a possibly-wrong API path. It
  drives the existing ``capydb`` CLI / competitor CLIs instead of reinventing a client.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    # Comparison metadata — the axes charts group by.
    tier_usd: float | None = None
    pg_version: str | None = None
    region: str | None = None
    # Provider key selects which [control.<provider>] block drives lifecycle ops.
    provider: str = "capydb"
    # Provider-scoped identifier (e.g. CapyDB project slug) for control templates.
    project: str | None = None

    def dsn(self) -> str:
        """libpq connection string for psycopg."""
        parts = [
            f"host={self.host}",
            f"port={self.port}",
            f"user={self.user}",
            f"dbname={self.dbname}",
            f"sslmode={self.sslmode}",
        ]
        if self.password is not None:
            parts.append(f"password={self.password}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ThroughputConfig:
    scale: int = 50               # pgbench -s / rows generated
    duration_s: int = 60          # per data-point run length
    concurrencies: tuple[int, ...] = (1, 8, 16, 32, 64)
    sysbench_percentile: int = 99
    read_only: bool = False       # also run a read-only variant when True


@dataclass(frozen=True, slots=True)
class NoisyNeighborConfig:
    victim: str                   # target name for the well-behaved workload
    aggressor: str                # target name (ideally same host) generating the storm
    victim_concurrency: int = 16
    aggressor_concurrency: int = 128
    duration_s: int = 60
    sysbench_percentile: int = 99


@dataclass(frozen=True, slots=True)
class BranchSpeedConfig:
    target: str                   # parent target name
    parent_sizes_mb: tuple[int, ...] = (1024, 10240, 102400)
    repeats: int = 3              # branch creations per size for a distribution


@dataclass(frozen=True, slots=True)
class ColdStartConfig:
    target: str
    repeats: int = 30
    settle_s: int = 5             # wait after issuing sleep before the wake connect


@dataclass(frozen=True, slots=True)
class ControlProvider:
    """Shell command templates that drive a provider's lifecycle.

    Templates are ``str.format``-expanded with scenario variables (e.g. ``{project}``,
    ``{branch}``). Each command must emit a single JSON object on stdout. A template left
    empty makes the dependent scenario fail loudly rather than guess an API path.
    """

    branch_create: str = ""       # vars: {project} {branch}; JSON must expose branch DSN fields
    branch_delete: str = ""       # vars: {project} {branch}
    sleep: str = ""               # vars: {project}
    wake: str = ""                # vars: {project}


@dataclass(frozen=True, slots=True)
class Suite:
    results_dsn: str
    targets: dict[str, Target]
    control: dict[str, ControlProvider] = field(default_factory=dict)
    throughput: ThroughputConfig | None = None
    noisy_neighbor: NoisyNeighborConfig | None = None
    branch_speed: BranchSpeedConfig | None = None
    cold_start: ColdStartConfig | None = None
    notes: str | None = None

    def target(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.targets)) or "(none)"
            raise KeyError(f"target {name!r} not defined; known targets: {known}") from exc

    def control_for(self, target: Target) -> ControlProvider:
        try:
            return self.control[target.provider]
        except KeyError as exc:
            raise KeyError(
                f"no [control.{target.provider}] block for target {target.name!r}"
            ) from exc


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
        tier_usd=_opt_float("tier_usd"),
        pg_version=raw.get("pg_version") if isinstance(raw.get("pg_version"), str) else None,
        region=raw.get("region") if isinstance(raw.get("region"), str) else None,
        provider=_req_str("provider", "capydb"),
        project=project,
    )


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

    control: dict[str, ControlProvider] = {}
    for provider, raw in (data.get("control") or {}).items():
        if not isinstance(raw, dict):
            continue
        commands = raw.get("commands", {})
        if not isinstance(commands, dict):
            raise TypeError(f"[control.{provider}.commands] must be a table")
        control[provider] = ControlProvider(
            branch_create=str(commands.get("branch_create", "")),
            branch_delete=str(commands.get("branch_delete", "")),
            sleep=str(commands.get("sleep", "")),
            wake=str(commands.get("wake", "")),
        )

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

    return Suite(
        results_dsn=results["dsn"],
        targets=targets,
        control=control,
        throughput=throughput,
        noisy_neighbor=noisy,
        branch_speed=branch,
        cold_start=cold,
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
    )
