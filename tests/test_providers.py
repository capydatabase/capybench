"""Unit tests for provider selection and adapter wiring (capybench.providers).

No network, no live platforms: these cover registry resolution, capability flags,
settings parsing, and pure mapping logic (branch URL -> Target).
"""

import pytest

from capybench import providers
from capybench.capabilities import Capability
from capybench.config import ProviderConfig, Suite, Target
from capybench.providers import CapyDBProvider, GenericProvider, Provider, ProviderError
from capybench.providers.capydb import branch_target_from_url
from capybench.scenarios.branch_speed import _branch_name


def _suite(**overrides) -> Suite:
    defaults = {
        "results_dsn": "host=localhost dbname=capybench",
        "targets": {},
    }
    defaults.update(overrides)
    return Suite(**defaults)


def _target(name: str = "t", **overrides) -> Target:
    defaults = {"name": name, "host": "db.example.com"}
    defaults.update(overrides)
    return Target(**defaults)


# -- registry / selection ---------------------------------------------------------------


def test_registered_types() -> None:
    assert providers.registered_types() == ("capydb", "generic")


def test_create_generic() -> None:
    provider = providers.create(ProviderConfig(name="generic", type="generic"))
    assert isinstance(provider, GenericProvider)
    assert provider.name == "generic"
    # A plain DSN has no lifecycle control at all - that is the whole point of `generic`.
    assert provider.capabilities == frozenset()
    assert not provider.supports(Capability.BRANCH)


def test_create_capydb_with_aliased_block_name() -> None:
    provider = providers.create(ProviderConfig(name="mycloud", type="capydb"))
    assert isinstance(provider, CapyDBProvider)
    assert provider.name == "mycloud"
    assert provider.capabilities == frozenset(
        {Capability.PROVISION, Capability.BRANCH, Capability.SLEEP, Capability.RESTORE}
    )
    assert provider.supports(Capability.RESTORE)


def test_create_unknown_type_lists_registered() -> None:
    with pytest.raises(ValueError, match="registered types: capydb, generic"):
        providers.create(ProviderConfig(name="neon", type="neon"))


def test_for_target_defaults_to_generic() -> None:
    target = _target()  # no provider key -> "generic"
    suite = _suite(targets={target.name: target})
    assert isinstance(providers.for_target(suite, target), GenericProvider)


def test_for_target_uses_named_block_settings() -> None:
    target = _target(provider="mycapy", project="prj_1")
    suite = _suite(
        targets={target.name: target},
        providers={
            "mycapy": ProviderConfig(
                name="mycapy", type="capydb", settings={"api_url": "https://api.x.dev/"}
            )
        },
    )
    provider = providers.for_target(suite, target)
    assert isinstance(provider, CapyDBProvider)
    assert provider.api_url == "https://api.x.dev"  # trailing slash stripped


# -- generic: unsupported ops fail loudly (scenarios gate on the flags first) -------------


def test_generic_lifecycle_ops_raise() -> None:
    provider = GenericProvider("generic", {})
    target = _target()
    with pytest.raises(ProviderError, match="does not support branch creation"):
        provider.create_branch(target, "b1")
    with pytest.raises(ProviderError, match="does not support branch deletion"):
        provider.delete_branch(target, "b1")
    with pytest.raises(ProviderError, match="does not support forced sleep"):
        provider.trigger_sleep(target)
    with pytest.raises(ProviderError, match="does not support database provisioning"):
        provider.provision("p1")
    with pytest.raises(ProviderError, match="does not support database destruction"):
        provider.destroy(target)
    with pytest.raises(ProviderError, match="does not support restore"):
        provider.restore(target, "r1")


# -- capydb: settings parsing and precondition errors (no network involved) --------------


def test_capydb_default_settings() -> None:
    provider = CapyDBProvider("capydb", {})
    assert provider.api_url == "https://api.capydb.dev"
    assert provider.token_env == "CAPYDB_TOKEN"
    assert provider.branch_ttl_hours == 2


def test_capydb_bad_setting_types_rejected() -> None:
    with pytest.raises(ProviderError, match="'branch_ttl_hours' must be an integer"):
        CapyDBProvider("capydb", {"branch_ttl_hours": "two"})
    with pytest.raises(ProviderError, match="'api_url' must be a non-empty string"):
        CapyDBProvider("capydb", {"api_url": ""})


def test_capydb_requires_project_before_any_api_call() -> None:
    provider = CapyDBProvider("capydb", {})
    target = _target(provider="capydb")  # project not set
    with pytest.raises(ProviderError, match="needs 'project' set"):
        provider.trigger_sleep(target)
    with pytest.raises(ProviderError, match="needs 'project' set"):
        provider.create_branch(target, "b1")


def test_capydb_requires_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPYBENCH_TEST_TOKEN", raising=False)
    provider = CapyDBProvider("capydb", {"token_env": "CAPYBENCH_TEST_TOKEN"})
    target = _target(provider="capydb", project="prj_1")
    with pytest.raises(ProviderError, match="CAPYBENCH_TEST_TOKEN"):
        provider.trigger_sleep(target)


# -- branch URL -> Target mapping ---------------------------------------------------------


def test_branch_target_from_url_full() -> None:
    parent = _target(
        name="parent",
        provider="capydb",
        project="prj_1",
        tier_usd=15.0,
        pg_version="17",
        region="eu-central",
    )
    url = "postgres://user%40x:p%40ss@branch.db.example.com:6432/branchdb?sslmode=require"
    branch = branch_target_from_url(parent, "b1", url)
    assert branch.name == "parent:b1"
    assert branch.host == "branch.db.example.com"
    assert branch.port == 6432
    assert branch.user == "user@x"  # percent-decoded
    assert branch.password == "p@ss"
    assert branch.dbname == "branchdb"
    assert branch.sslmode == "require"
    # comparison metadata is inherited from the parent
    assert branch.tier_usd == 15.0
    assert branch.pg_version == "17"
    assert branch.region == "eu-central"
    assert branch.provider == "capydb"
    assert branch.project == "prj_1"


def test_branch_target_from_url_falls_back_to_parent_fields() -> None:
    parent = _target(name="parent", port=5433, user="app", dbname="appdb", password="pw")
    branch = branch_target_from_url(parent, "b1", "postgres://h.example.com")
    assert branch.host == "h.example.com"
    assert branch.port == 5433
    assert branch.user == "app"
    assert branch.dbname == "appdb"
    assert branch.password == "pw"


def test_branch_target_from_url_rejects_hostless_url() -> None:
    parent = _target(name="parent")
    with pytest.raises(ProviderError, match="unparseable"):
        branch_target_from_url(parent, "b1", "not a url")


# -- interface shape ----------------------------------------------------------------------


def test_provider_base_defaults() -> None:
    class Dummy(Provider):
        type = "dummy"

    dummy = Dummy("d", {"k": "v"})
    assert dummy.settings == {"k": "v"}
    # Capabilities are opt-in: a new adapter claims nothing until it implements something.
    assert dummy.capabilities == frozenset()
    assert all(not dummy.supports(cap) for cap in Capability)


def test_branch_names_are_unique_per_run() -> None:
    first = _branch_name("11111111-aaaa-bbbb-cccc-111111111111", 1024, 0)
    second = _branch_name("22222222-aaaa-bbbb-cccc-222222222222", 1024, 0)
    assert first != second
    assert first.startswith("capybench-1024mb-0-")
