"""Unit tests for TOML suite parsing (capybench.config)."""

from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from capybench import config

MINIMAL = """
[results]
dsn = "host=localhost dbname=capybench"

[targets.plain]
host = "db.example.com"
"""

FULL = """
notes = "test run"
client_region = "hel1"

[results]
dsn = "host=localhost dbname=capybench"

[providers.capydb]
api_url = "https://api.example.dev"
token_env = "MY_TOKEN"
branch_ttl_hours = 4

[providers.mycloud]
type = "capydb"

[targets.main]
host = "main.example.com"
port = 6432
user = "app"
dbname = "app"
sslmode = "require"
tier_usd = 15
pg_version = "17"
region = "eu-central"
provider = "capydb"
project = "prj_123"

[targets.plain]
host = "other.example.com"

[throughput]
scale = 10
duration_s = 30
concurrencies = [1, 4]

[noisy_neighbor]
victim = "main"
aggressor = "plain"

[branch_speed]
target = "main"
parent_sizes_mb = [128]

[cold_start]
target = "main"
repeats = 5

[query_latency]
targets = ["main"]
repeats = 200
connect_repeats = 10
rows = 5000

[fact_sheet]
probe_gucs = false

[connection_ceiling]
max_probe = 25

[sustained]
duration_s = 300
window_s = 30
concurrency = 8

[vacuum_under_write]
target = "main"
duration_s = 120

[cold_cache]
size_mb = 64
repeats = 2

[provision_speed]
provider = "capydb"
repeats = 2
pg_version = "18"
region = "eu-central"
tier_usd = 15

[restore_rto]
target = "main"
repeats = 2
restore_time = "2026-08-17T10:00:00Z"

[host_probe]
type = "ssh"
host = "db1.example.com"
cgroup = "/sys/fs/cgroup/system.slice/pg.service"
label = "db1"

[attestation]
node = "ccx23 - 4 dedicated vCPU / 16 GB"
colocated = true
tenants_on_node = 18
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "capybench.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_minimal_config_defaults(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, MINIMAL))
    target = suite.target("plain")
    assert target.port == 5432
    assert target.user == "postgres"
    assert target.provider == "generic"  # default: plain DSN, no lifecycle ops
    assert target.project is None
    assert suite.providers == {}
    assert suite.client_region is None
    assert suite.throughput is None
    assert suite.cold_start is None
    assert suite.query_latency is None


def test_query_latency_defaults_to_all_targets(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, MINIMAL + "\n[query_latency]\n"))
    assert suite.query_latency is not None
    assert suite.query_latency.targets is None  # None = run against every target
    assert suite.query_latency.repeats == 500


def test_query_latency_bad_targets_rejected(tmp_path: Path) -> None:
    body = MINIMAL + "\n[query_latency]\ntargets = [1, 2]\n"
    with pytest.raises(TypeError, match="list of strings"):
        config.load(_write(tmp_path, body))


def test_full_config_parses(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, FULL))
    assert suite.notes == "test run"
    assert suite.client_region == "hel1"

    main = suite.target("main")
    assert main.provider == "capydb"
    assert main.project == "prj_123"
    assert main.tier_usd == 15.0
    assert "password" not in main.dsn()

    assert suite.throughput is not None
    assert suite.throughput.concurrencies == (1, 4)
    assert suite.branch_speed is not None
    assert suite.branch_speed.parent_sizes_mb == (128,)
    assert suite.cold_start is not None
    assert suite.cold_start.repeats == 5
    assert suite.query_latency is not None
    assert suite.query_latency.targets == ("main",)
    assert suite.query_latency.repeats == 200
    assert suite.query_latency.connect_repeats == 10
    assert suite.query_latency.rows == 5000


def test_provider_blocks_carry_type_and_settings(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, FULL))

    capydb = suite.providers["capydb"]
    assert capydb.type == "capydb"  # type defaults to the block name
    assert capydb.settings == {
        "api_url": "https://api.example.dev",
        "token_env": "MY_TOKEN",
        "branch_ttl_hours": 4,
    }

    aliased = suite.providers["mycloud"]
    assert aliased.name == "mycloud"
    assert aliased.type == "capydb"  # explicit type overrides the block name
    assert aliased.settings == {}


def test_provider_config_synthesized_when_block_missing(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, MINIMAL))
    cfg = suite.provider_config(suite.target("plain"))
    assert cfg.name == "generic"
    assert cfg.type == "generic"
    assert cfg.settings == {}


def test_unknown_target_lists_known_names(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, MINIMAL))
    with pytest.raises(KeyError, match="known targets: plain"):
        suite.target("nope")


def test_missing_results_section_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\[results\]"):
        config.load(_write(tmp_path, '[targets.a]\nhost = "h"\n'))


def test_missing_targets_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        config.load(_write(tmp_path, '[results]\ndsn = "host=x"\n'))


def test_bad_port_type_rejected(tmp_path: Path) -> None:
    body = MINIMAL + 'port = "5432"\n'
    with pytest.raises(TypeError, match="'port' must be an integer"):
        config.load(_write(tmp_path, body))


def test_bad_provider_type_rejected(tmp_path: Path) -> None:
    body = MINIMAL + "\n[providers.x]\ntype = 5\n"
    with pytest.raises(TypeError, match="'type' must be a string"):
        config.load(_write(tmp_path, body))


def test_bad_client_region_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="client_region"):
        config.load(_write(tmp_path, "client_region = 3\n" + MINIMAL))


def test_dsn_includes_password_only_when_set(tmp_path: Path) -> None:
    body = MINIMAL + 'password = "s3cret"\n'
    suite = config.load(_write(tmp_path, body))
    assert "password=s3cret" in suite.target("plain").dsn()


def test_sslrootcert_optional_and_in_dsn(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, MINIMAL))
    assert suite.target("plain").sslrootcert is None
    assert "sslrootcert" not in suite.target("plain").dsn()

    body = MINIMAL + 'sslrootcert = "system"\n'
    target = config.load(_write(tmp_path, body)).target("plain")
    assert target.sslrootcert == "system"
    assert "sslrootcert=system" in target.dsn()


def test_new_scenario_sections_parse(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, FULL))

    assert suite.fact_sheet is not None and suite.fact_sheet.probe_gucs is False
    assert suite.connection_ceiling is not None and suite.connection_ceiling.max_probe == 25
    assert suite.sustained is not None
    assert (suite.sustained.duration_s, suite.sustained.window_s) == (300, 30)
    assert suite.vacuum_under_write is not None
    assert suite.vacuum_under_write.target == "main"
    assert suite.cold_cache is not None and suite.cold_cache.size_mb == 64
    assert suite.provision_speed is not None
    assert suite.provision_speed.provider == "capydb"
    assert suite.provision_speed.tier_usd == 15.0
    assert suite.restore_rto is not None
    assert suite.restore_rto.restore_time == "2026-08-17T10:00:00Z"


def test_scenario_sections_are_absent_unless_declared(tmp_path: Path) -> None:
    """Omitting a section is how you choose what to run; nothing defaults itself on."""
    suite = config.load(_write(tmp_path, MINIMAL))
    for name in (
        "fact_sheet",
        "connection_ceiling",
        "sustained",
        "vacuum_under_write",
        "cold_cache",
        "provision_speed",
        "restore_rto",
    ):
        assert getattr(suite, name) is None, name


def test_host_probe_block_parses(tmp_path: Path) -> None:
    suite = config.load(_write(tmp_path, FULL))
    assert suite.host_probe is not None
    assert suite.host_probe.type == "ssh"
    assert suite.host_probe.settings["host"] == "db1.example.com"
    assert suite.host_probe.settings["label"] == "db1"
    assert "type" not in suite.host_probe.settings  # type is not passed through as a setting


def test_host_probe_type_defaults_to_ssh(tmp_path: Path) -> None:
    body = MINIMAL + '\n[host_probe]\nhost = "n1"\n'
    suite = config.load(_write(tmp_path, body))
    assert suite.host_probe is not None and suite.host_probe.type == "ssh"


def test_host_probe_absent_by_default(tmp_path: Path) -> None:
    assert config.load(_write(tmp_path, MINIMAL)).host_probe is None


def test_attestation_values_are_stringified(tmp_path: Path) -> None:
    """Declared facts are published verbatim as text, whatever TOML type they were."""
    suite = config.load(_write(tmp_path, FULL))
    assert suite.attestation["node"] == "ccx23 - 4 dedicated vCPU / 16 GB"
    assert suite.attestation["colocated"] == "True"
    assert suite.attestation["tenants_on_node"] == "18"


def test_attestation_rejects_nested_tables(tmp_path: Path) -> None:
    body = MINIMAL + "\n[attestation]\n[attestation.nested]\nx = 1\n"
    with pytest.raises(TypeError, match="must be a scalar"):
        config.load(_write(tmp_path, body))


def test_attestation_empty_by_default(tmp_path: Path) -> None:
    assert config.load(_write(tmp_path, MINIMAL)).attestation == {}


def test_scenario_requiring_a_target_rejects_a_missing_key(tmp_path: Path) -> None:
    body = MINIMAL + "\n[restore_rto]\nrepeats = 2\n"
    with pytest.raises(TypeError, match="requires 'target'"):
        config.load(_write(tmp_path, body))


def test_provision_speed_requires_a_provider(tmp_path: Path) -> None:
    body = MINIMAL + "\n[provision_speed]\nrepeats = 2\n"
    with pytest.raises(TypeError, match="requires 'provider'"):
        config.load(_write(tmp_path, body))


def test_provision_speed_rejects_non_numeric_tier(tmp_path: Path) -> None:
    body = MINIMAL + '\n[provision_speed]\nprovider = "p"\ntier_usd = "free"\n'
    with pytest.raises(TypeError, match="'tier_usd' must be a number"):
        config.load(_write(tmp_path, body))


def test_empty_targets_list_means_every_target(tmp_path: Path) -> None:
    body = MINIMAL + "\n[cold_cache]\ntargets = []\n"
    suite = config.load(_write(tmp_path, body))
    assert suite.cold_cache is not None and suite.cold_cache.targets is None


def test_dsn_escapes_credentials_as_one_parameter(tmp_path: Path) -> None:
    body = MINIMAL + 'password = "space sslmode=disable\\\\quote\'"\n'
    target = config.load(_write(tmp_path, body)).target("plain")
    parsed = conninfo_to_dict(target.dsn())
    assert parsed["password"] == "space sslmode=disable\\quote'"
    assert parsed["sslmode"] == "prefer"
