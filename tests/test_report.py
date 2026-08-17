"""Unit tests for report rendering (the pure HTML builders - no database involved).

The report is the artifact that gets published, so two things matter beyond "it renders":
values coming from a target must be escaped rather than injected, and unverifiable
material (attestation, host-vantage readings) must be visibly labelled as such.
"""

from __future__ import annotations

from capybench.report import _attestation_section, _facts_table, _host_section

FACTS = [
    # (category, key, target, value, source)
    ("durability", "durability_posture", "pg-a", "full", "derived"),
    ("durability", "durability_posture", "pg-b", "relaxed", "derived"),
    ("durability", "fsync", "pg-a", "on", "pg_settings"),
    ("durability", "fsync", "pg-b", "off", "pg_settings"),
    ("pooling", "backend_pid_stable", "pg-a", "true", "probe"),
    ("extensions", "ext_vector", "pg-a", "installed 0.8.5", "pg_settings"),
]


def test_facts_table_pivots_targets_into_columns() -> None:
    html = _facts_table(FACTS)
    assert "<th>pg-a</th>" in html and "<th>pg-b</th>" in html
    # One row per fact, both targets' values side by side for comparison.
    assert "<td>full</td>" in html and "<td>relaxed</td>" in html


def test_facts_table_orders_durability_first() -> None:
    """The posture that decides whether numbers are comparable leads the table."""
    html = _facts_table(FACTS)
    assert html.index("durability_posture") < html.index("ext_vector")


def test_facts_table_shows_the_source_of_each_fact() -> None:
    html = _facts_table(FACTS)
    assert "probe" in html and "pg_settings" in html


def test_facts_table_fills_missing_targets_with_a_dash() -> None:
    """pg-b has no pooling probe here; the cell must be empty, not silently borrowed."""
    html = _facts_table(FACTS)
    assert "<td>-</td>" in html


def test_facts_table_without_facts_prompts_for_the_block() -> None:
    html = _facts_table([])
    assert "fact_sheet" in html
    assert "<table>" not in html


def test_fact_values_are_escaped() -> None:
    """A value read from a target's settings must never become markup."""
    html = _facts_table([("other", "evil", "t", "<script>alert(1)</script>", "pg_settings")])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_attestation_is_labelled_as_unverified() -> None:
    html = _attestation_section({"colocated": "true", "node": "ccx23"})
    assert "declared" in html.lower()
    assert "cannot verify" in html
    assert "ccx23" in html


def test_attestation_absent_renders_nothing() -> None:
    assert _attestation_section({}) == ""


def test_attestation_values_are_escaped() -> None:
    html = _attestation_section({"node": "<b>x</b>"})
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html


def test_host_section_marks_readings_as_non_reproducible() -> None:
    host = {
        "samples": [("noisy_neighbor", "db1", "cgroup_cpu_throttled_usec", "us", 4100000.0)],
        "facts": [("db1", "kernel", "6.1.0-21-amd64")],
    }
    html = _host_section(host)
    assert "privileged" in html.lower()
    assert "cannot reproduce" in html
    assert "cgroup_cpu_throttled_usec" in html
    assert "6.1.0-21-amd64" in html


def test_host_section_absent_renders_nothing() -> None:
    assert _host_section({"samples": [], "facts": []}) == ""


def test_host_section_handles_facts_without_samples() -> None:
    html = _host_section({"samples": [], "facts": [("db1", "cpu_count", "8")]})
    assert "cpu_count" in html
    assert "Change during each scenario" not in html
