"""Unit tests for the host probe's parsing and aggregation.

The SSH transport is not exercised here (no live node), but everything it feeds is: the
parsers run against real ``/proc`` and ``arcstats`` output, so a format assumption that is
wrong fails here rather than silently producing zeros in a published report.
"""

from __future__ import annotations

import pytest

from capybench.config import HostProbeConfig
from capybench.hostprobe import Reading, create, deltas, registered_types
from capybench.hostprobe.base import HostProbeError
from capybench.hostprobe.ssh import (
    SSHHostProbe,
    parse_arcstats,
    parse_keyed_values,
    parse_meminfo,
    parse_pressure,
    parse_sections,
    readings_from_sections,
)

# Real output shapes, copied from a Linux node.
LOADAVG = "0.52 0.58 0.59 2/1234 56789"
MEMINFO = """\
MemTotal:       16384000 kB
MemFree:         1024000 kB
MemAvailable:    8192000 kB
Buffers:          131072 kB
"""
PRESSURE_CPU = "some avg10=0.00 avg60=0.13 avg300=0.07 total=12345678\n"
PRESSURE_IO = """\
some avg10=0.12 avg60=0.05 avg300=0.01 total=987654321
full avg10=0.05 avg60=0.02 avg300=0.00 total=123456789
"""
CGROUP_CPU = """\
usage_usec 123456789
user_usec 98765432
system_usec 24691357
nr_periods 10000
nr_throttled 42
throttled_usec 1234567
"""
ARCSTATS = """\
name                            type data
hits                            4    123456789
misses                          4    987654
size                            4    4294967296
c_max                           4    8589934592
"""


def _sections(**overrides: str) -> dict[str, str]:
    base = {
        "loadavg": LOADAVG,
        "meminfo": MEMINFO,
        "pressure_cpu": PRESSURE_CPU,
        "pressure_io": PRESSURE_IO,
        "cgroup_cpu": CGROUP_CPU,
        "cgroup_memory_current": "2147483648\n",
        "arcstats": ARCSTATS,
    }
    base.update(overrides)
    return base


# -- section splitting -------------------------------------------------------------------


def test_parse_sections_splits_on_markers() -> None:
    output = "@@uname\n6.1.0-21-amd64\n@@nproc\n8\n@@loadavg\n" + LOADAVG
    sections = parse_sections(output)
    assert sections["uname"] == "6.1.0-21-amd64"
    assert sections["nproc"] == "8"
    assert sections["loadavg"] == LOADAVG


def test_parse_sections_keeps_empty_sections_empty() -> None:
    """A missing file yields an empty section, not a missing key or a crash."""
    sections = parse_sections("@@arcstats\n@@nproc\n4\n")
    assert sections["arcstats"] == ""
    assert sections["nproc"] == "4"


# -- individual parsers ------------------------------------------------------------------


def test_parse_meminfo_converts_kb_to_bytes() -> None:
    mem = parse_meminfo(MEMINFO)
    assert mem["MemTotal"] == 16384000 * 1024
    assert mem["MemAvailable"] == 8192000 * 1024


def test_parse_pressure_reads_totals_per_scope() -> None:
    assert parse_pressure(PRESSURE_CPU) == {"some": 12345678.0}
    assert parse_pressure(PRESSURE_IO) == {"some": 987654321.0, "full": 123456789.0}


def test_parse_keyed_values_reads_cgroup_cpu_stat() -> None:
    cpu = parse_keyed_values(CGROUP_CPU)
    assert cpu["throttled_usec"] == 1234567.0
    assert cpu["nr_throttled"] == 42.0


def test_parse_arcstats_uses_the_third_column() -> None:
    arc = parse_arcstats(ARCSTATS)
    assert arc["hits"] == 123456789.0
    assert arc["misses"] == 987654.0
    assert arc["size"] == 4294967296.0
    assert "name" not in arc  # the header row is not a statistic


# -- assembly ----------------------------------------------------------------------------


def test_readings_cover_every_source() -> None:
    readings = readings_from_sections(_sections())
    assert readings["load1"] == Reading(0.52, "load")
    assert readings["mem_available_bytes"].value == 8192000 * 1024
    assert readings["psi_cpu_some_us"] == Reading(12345678.0, "us", counter=True)
    assert readings["psi_io_full_us"] == Reading(123456789.0, "us", counter=True)
    assert readings["cgroup_cpu_throttled_usec"] == Reading(1234567.0, "us", counter=True)
    assert readings["cgroup_memory_current_bytes"].value == 2147483648.0
    assert readings["zfs_arc_hits"].counter is True
    assert readings["zfs_arc_size_bytes"].counter is False


def test_counters_and_gauges_are_distinguished() -> None:
    """Deltas only make sense for counters; a gauge delta would be meaningless."""
    readings = readings_from_sections(_sections())
    assert readings["cgroup_cpu_usage_usec"].counter is True
    assert readings["load1"].counter is False
    assert readings["mem_available_bytes"].counter is False


def test_missing_interfaces_are_skipped_not_faked() -> None:
    """A node without ZFS or cgroups reports fewer metrics - never zeros."""
    readings = readings_from_sections(
        _sections(arcstats="", cgroup_cpu="", cgroup_memory_current="")
    )
    assert not any(key.startswith("zfs_") for key in readings)
    assert not any(key.startswith("cgroup_") for key in readings)
    assert "load1" in readings  # what is available is still collected


def test_readings_from_empty_output_is_empty() -> None:
    assert readings_from_sections({}) == {}


# -- deltas ------------------------------------------------------------------------------


def test_deltas_subtract_counters_only() -> None:
    before = {
        "c": Reading(100.0, "us", counter=True),
        "g": Reading(5.0, "bytes"),
    }
    after = {
        "c": Reading(175.0, "us", counter=True),
        "g": Reading(9.0, "bytes"),
    }
    result = deltas(before, after)
    assert result == {"c": Reading(75.0, "us", counter=True)}
    assert "g" not in result  # gauges are reported as before/after, not differenced


def test_delta_dropped_when_counter_resets() -> None:
    """A counter going backwards means the node or cgroup restarted mid-scenario."""
    before = {"c": Reading(500.0, "us", counter=True)}
    after = {"c": Reading(12.0, "us", counter=True)}
    assert deltas(before, after) == {}


def test_delta_skips_metrics_absent_from_the_first_sample() -> None:
    after = {"new": Reading(10.0, "us", counter=True)}
    assert deltas({}, after) == {}


# -- configuration -----------------------------------------------------------------------


def test_registry_exposes_ssh() -> None:
    assert registered_types() == ("ssh",)


def test_create_unknown_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown host_probe type"):
        create(HostProbeConfig(name="x", type="telepathy"))


def test_ssh_probe_requires_a_host() -> None:
    with pytest.raises(HostProbeError, match="'host' is required"):
        SSHHostProbe("ssh", {})


def test_ssh_probe_defaults_and_label() -> None:
    probe = SSHHostProbe("ssh", {"host": "db1.example.com"})
    assert probe.user == "root"
    assert probe.port == 22
    assert probe.label == "db1.example.com"  # falls back to the host
    assert probe.cgroup == ""


def test_ssh_probe_label_overrides_host() -> None:
    probe = SSHHostProbe("ssh", {"host": "10.0.0.5", "label": "db1"})
    assert probe.label == "db1"


def test_ssh_probe_rejects_bad_types() -> None:
    with pytest.raises(HostProbeError, match="'port' must be an integer"):
        SSHHostProbe("ssh", {"host": "h", "port": "22"})
    with pytest.raises(HostProbeError, match="'timeout_s' must be an integer"):
        SSHHostProbe("ssh", {"host": "h", "timeout_s": 1.5})


def test_cgroup_path_is_shell_quoted() -> None:
    """The cgroup path lands in a remote shell script; it must not be able to inject."""
    probe = SSHHostProbe("ssh", {"host": "h", "cgroup": "/sys/fs/cgroup/a b; rm -rf /"})
    script = probe._script()
    assert "'/sys/fs/cgroup/a b; rm -rf /'" in script
    assert "; rm -rf /\n" not in script


def test_script_omits_cgroup_section_when_unset() -> None:
    probe = SSHHostProbe("ssh", {"host": "h"})
    assert "cgroup_cpu" not in probe._script()
