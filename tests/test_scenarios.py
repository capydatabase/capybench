"""Unit tests for the scenario registry and the pure logic inside scenarios.

The registry is where capability gating lives now, so these tests cover the decisions the
runner makes before any database is touched: what runs, what skips, and why. The
interpretation helpers (`drift`, `durability_posture`) are tested here too - they are the
functions that turn raw numbers into a published claim, so being wrong is expensive.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager

import pytest

from capybench import scenarios
from capybench.config import (
    BranchSpeedConfig,
    ColdStartConfig,
    ProviderConfig,
    ProvisionSpeedConfig,
    QueryLatencyConfig,
    RestoreRTOConfig,
    Suite,
    Target,
    ThroughputConfig,
)
from capybench.scenarios import ScenarioSpec
from capybench.scenarios._naming import run_token
from capybench.scenarios.fact_sheet import durability_posture
from capybench.scenarios.sustained import drift


def _suite(**overrides) -> Suite:
    defaults = {"results_dsn": "host=localhost dbname=capybench", "targets": {}}
    defaults.update(overrides)
    return Suite(**defaults)


def _with_targets(provider: str = "generic", **provider_blocks) -> Suite:
    target = Target(name="t", host="db.example.com", provider=provider, project="prj_1")
    return _suite(targets={"t": target}, providers=provider_blocks)


# -- registry shape ----------------------------------------------------------------------


def test_every_spec_maps_to_a_real_suite_field() -> None:
    """A typo'd config_attr would make a scenario silently never run."""
    suite_fields = {field.name for field in dataclasses.fields(Suite)}
    for spec in scenarios.SPECS:
        assert spec.config_attr in suite_fields, f"{spec.name}: no Suite.{spec.config_attr}"


def test_every_spec_resolves_a_field_on_its_own_config() -> None:
    """A capability-gated scenario must name where its target/provider comes from."""
    config_types = {
        "branch_speed": BranchSpeedConfig,
        "cold_start": ColdStartConfig,
        "restore_rto": RestoreRTOConfig,
        "provision_speed": ProvisionSpeedConfig,
    }
    for spec in scenarios.SPECS:
        if not spec.requires:
            assert spec.resolves is None and spec.scope == "none"
            continue
        assert spec.resolves is not None
        fields = {f.name for f in dataclasses.fields(config_types[spec.name])}
        assert spec.resolves in fields, f"{spec.name}: config has no {spec.resolves!r}"


def test_scenario_names_are_unique_and_lookupable() -> None:
    assert len(scenarios.NAMES) == len(set(scenarios.NAMES))
    for name in scenarios.NAMES:
        assert scenarios.spec(name).name == name


def test_unknown_scenario_lookup_lists_known_names() -> None:
    with pytest.raises(KeyError, match="unknown scenario"):
        scenarios.spec("nope")


def test_disruptive_scenarios_run_last() -> None:
    """Sleeping or restoring a target must not skew the measurements that follow it."""
    order = list(scenarios.NAMES)
    for disruptive in ("branch_speed", "restore_rto", "provision_speed", "cold_start"):
        for observational in ("fact_sheet", "query_latency", "throughput", "sustained"):
            assert order.index(disruptive) > order.index(observational)
    assert order[0] == "fact_sheet"  # posture is recorded before anything perturbs it


# -- capability gating -------------------------------------------------------------------


def test_scenario_without_capabilities_is_always_supported() -> None:
    suite = _with_targets()
    suite = dataclasses.replace(suite, throughput=ThroughputConfig())
    assert scenarios.unsupported_reason(suite, scenarios.spec("throughput")) is None


def test_branch_speed_unsupported_on_generic_target() -> None:
    suite = dataclasses.replace(_with_targets(), branch_speed=BranchSpeedConfig(target="t"))
    reason = scenarios.unsupported_reason(suite, scenarios.spec("branch_speed"))
    assert reason is not None
    assert "branch_speed" in reason
    assert "needs branch" in reason
    assert "supports none" in reason  # generic advertises nothing


def test_branch_speed_supported_on_capable_provider() -> None:
    suite = _with_targets("capydb", capydb=ProviderConfig(name="capydb", type="capydb"))
    suite = dataclasses.replace(suite, branch_speed=BranchSpeedConfig(target="t"))
    assert scenarios.unsupported_reason(suite, scenarios.spec("branch_speed")) is None


def test_restore_and_sleep_gate_on_their_own_capabilities() -> None:
    generic = dataclasses.replace(
        _with_targets(),
        restore_rto=RestoreRTOConfig(target="t"),
        cold_start=ColdStartConfig(target="t"),
    )
    assert "needs restore" in (scenarios.unsupported_reason(generic, scenarios.spec("restore_rto")))
    assert "needs sleep" in (scenarios.unsupported_reason(generic, scenarios.spec("cold_start")))


def test_unconfigured_scenario_has_no_reason_to_report() -> None:
    """Not configured is a choice, not a failure - the runner skips it silently."""
    assert scenarios.unsupported_reason(_with_targets(), scenarios.spec("branch_speed")) is None


def test_provision_speed_resolves_a_provider_not_a_target() -> None:
    suite = dataclasses.replace(
        _suite(providers={"capydb": ProviderConfig(name="capydb", type="capydb")}),
        provision_speed=ProvisionSpeedConfig(provider="capydb"),
    )
    assert scenarios.unsupported_reason(suite, scenarios.spec("provision_speed")) is None


def test_provision_speed_missing_provider_block_degrades_to_a_skip() -> None:
    suite = dataclasses.replace(_suite(), provision_speed=ProvisionSpeedConfig(provider="nowhere"))
    reason = scenarios.unsupported_reason(suite, scenarios.spec("provision_speed"))
    assert reason is not None and "nowhere" in reason


def test_bad_provider_config_degrades_to_a_skip_not_a_crash() -> None:
    """A misconfigured block must not take down a run that is already in progress."""
    suite = _with_targets("broken", broken=ProviderConfig(name="broken", type="nonexistent"))
    suite = dataclasses.replace(suite, cold_start=ColdStartConfig(target="t"))
    reason = scenarios.unsupported_reason(suite, scenarios.spec("cold_start"))
    assert reason is not None and "unknown provider type" in reason


def test_unknown_target_reference_degrades_to_a_skip() -> None:
    suite = dataclasses.replace(_with_targets(), cold_start=ColdStartConfig(target="ghost"))
    reason = scenarios.unsupported_reason(suite, scenarios.spec("cold_start"))
    assert reason is not None and "ghost" in reason


# -- run_all orchestration ---------------------------------------------------------------


def _fake_spec(name: str, calls: list[str]) -> ScenarioSpec:
    def run(suite: Suite, run_obj: object, *, log) -> None:
        calls.append(name)

    return ScenarioSpec(name=name, run=run, config_attr=name)


def test_run_all_executes_configured_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(scenarios, "SPECS", (_fake_spec("throughput", calls),))
    suite = dataclasses.replace(_with_targets(), throughput=ThroughputConfig())

    executed = scenarios.run_all(suite, None, only={"throughput"}, log=lambda _: None)
    assert executed == ["throughput"] and calls == ["throughput"]


def test_run_all_skips_unconfigured_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    logged: list[str] = []
    monkeypatch.setattr(scenarios, "SPECS", (_fake_spec("throughput", calls),))

    executed = scenarios.run_all(_with_targets(), None, only={"throughput"}, log=logged.append)
    assert executed == [] and calls == [] and logged == []


def test_run_all_skips_unsupported_loudly() -> None:
    logged: list[str] = []
    suite = dataclasses.replace(_with_targets(), branch_speed=BranchSpeedConfig(target="t"))

    executed = scenarios.run_all(suite, None, only={"branch_speed"}, log=logged.append)
    assert executed == []
    assert any("skipping" in line and "needs branch" in line for line in logged)


def test_run_all_honors_the_only_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        scenarios,
        "SPECS",
        (_fake_spec("throughput", calls), _fake_spec("query_latency", calls)),
    )
    suite = dataclasses.replace(
        _with_targets(),
        throughput=ThroughputConfig(),
        query_latency=QueryLatencyConfig(),
    )
    executed = scenarios.run_all(suite, None, only={"throughput"}, log=lambda _: None)
    assert executed == ["throughput"] and calls == ["throughput"]


def test_run_all_wraps_each_scenario_with_around(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host-vantage readings must bracket exactly the window a scenario occupies."""
    events: list[str] = []
    monkeypatch.setattr(scenarios, "SPECS", (_fake_spec("throughput", events),))

    @contextmanager
    def around(name: str):
        events.append(f"enter:{name}")
        yield
        events.append(f"exit:{name}")

    suite = dataclasses.replace(_with_targets(), throughput=ThroughputConfig())
    scenarios.run_all(suite, None, only={"throughput"}, log=lambda _: None, around=around)
    assert events == ["enter:throughput", "throughput", "exit:throughput"]


def test_a_failing_scenario_does_not_kill_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """An hour-long matrix must not be lost because one target went away mid-sweep."""
    calls: list[str] = []
    logged: list[str] = []

    def boom(suite: Suite, run_obj: object, *, log) -> None:
        raise RuntimeError("target vanished")

    monkeypatch.setattr(
        scenarios,
        "SPECS",
        (
            ScenarioSpec(name="throughput", run=boom, config_attr="throughput"),
            _fake_spec("query_latency", calls),
        ),
    )
    suite = dataclasses.replace(
        _with_targets(), throughput=ThroughputConfig(), query_latency=QueryLatencyConfig()
    )

    executed = scenarios.run_all(
        suite, None, only={"throughput", "query_latency"}, log=logged.append
    )
    assert executed == ["query_latency"]  # the failure is excluded, the rest still ran
    assert calls == ["query_latency"]
    assert any("throughput FAILED" in line and "target vanished" in line for line in logged)


def test_host_window_closes_even_when_a_scenario_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host readings must still be captured for a window that ended in an error."""
    events: list[str] = []

    def boom(suite: Suite, run_obj: object, *, log) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr(
        scenarios, "SPECS", (ScenarioSpec(name="throughput", run=boom, config_attr="throughput"),)
    )

    @contextmanager
    def around(name: str):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    suite = dataclasses.replace(_with_targets(), throughput=ThroughputConfig())
    scenarios.run_all(suite, None, only={"throughput"}, log=lambda _: None, around=around)
    assert events == ["enter", "exit"]


def test_around_is_not_entered_for_skipped_scenarios() -> None:
    """A skipped scenario has no window, so it must not produce host readings either."""
    events: list[str] = []

    @contextmanager
    def around(name: str):
        events.append(f"enter:{name}")
        yield

    suite = dataclasses.replace(_with_targets(), branch_speed=BranchSpeedConfig(target="t"))
    scenarios.run_all(suite, None, only={"branch_speed"}, log=lambda _: None, around=around)
    assert events == []


# -- run-scoped naming -------------------------------------------------------------------


def test_run_token_is_hex_only_and_bounded() -> None:
    token = run_token("11111111-aaaa-bbbb-cccc-111111111111")
    assert token == "11111111aa"
    assert len(token) == 10


def test_run_token_survives_a_non_hex_run_id() -> None:
    assert run_token("zzz") == "run"


def test_run_tokens_differ_per_run() -> None:
    assert run_token("11111111-aaaa") != run_token("22222222-aaaa")


# -- sustained: drift interpretation -----------------------------------------------------


def test_drift_is_zero_for_a_flat_series() -> None:
    shape = drift([100.0] * 20)
    assert shape["tps_drift_pct"] == pytest.approx(0.0)
    assert shape["tps_cv_pct"] == pytest.approx(0.0)


def test_drift_detects_a_burst_credit_cliff() -> None:
    """Fast for the first half, throttled for the second - the burstable signature."""
    series = [1000.0] * 10 + [250.0] * 10
    shape = drift(series)
    assert shape["tps_first_quarter"] == pytest.approx(1000.0)
    assert shape["tps_last_quarter"] == pytest.approx(250.0)
    assert shape["tps_drift_pct"] == pytest.approx(-75.0)
    assert shape["tps_min_window"] == 250.0


def test_drift_reports_improvement_as_positive() -> None:
    assert drift([100.0] * 4 + [200.0] * 4)["tps_drift_pct"] > 0


def test_drift_needs_four_windows() -> None:
    with pytest.raises(ValueError, match="at least 4 windows"):
        drift([1.0, 2.0, 3.0])


def test_drift_survives_a_zero_baseline() -> None:
    """A target that never started must not raise ZeroDivisionError mid-run."""
    assert drift([0.0, 0.0, 0.0, 0.0])["tps_drift_pct"] == 0.0


# -- fact_sheet: durability verdict ------------------------------------------------------


def test_durable_configuration_is_full() -> None:
    posture, _note = durability_posture(
        {"fsync": "on", "synchronous_commit": "on", "full_page_writes": "on"}
    )
    assert posture == "full"


def test_fsync_off_is_unsafe() -> None:
    posture, note = durability_posture({"fsync": "off", "synchronous_commit": "on"})
    assert posture == "unsafe"
    assert "lose" in note or "corrupt" in note


def test_async_commit_is_relaxed_and_says_why() -> None:
    posture, note = durability_posture(
        {"fsync": "on", "synchronous_commit": "off", "full_page_writes": "on"}
    )
    assert posture == "relaxed"
    assert "not comparable" in note


def test_unreadable_settings_are_unknown_not_assumed_safe() -> None:
    posture, _note = durability_posture({})
    assert posture == "unknown"
