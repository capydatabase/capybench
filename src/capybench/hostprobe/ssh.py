"""SSH host probe: node-level readings over a single SSH round trip.

Deliberately provider-neutral - it reads standard Linux interfaces, so any operator with
SSH to the node running their Postgres can use it, whatever platform they built:

* ``/proc/loadavg``, ``/proc/meminfo``
* ``/proc/pressure/{cpu,io,memory}`` - PSI stall totals, the honest "was this node
  starved" signal
* cgroup v2 ``cpu.stat`` / ``memory.current`` - per-tenant CPU throttling and footprint,
  which is exactly what a fair-share isolation claim rests on
* ZFS ``arcstats`` when present - cache hit ratio, i.e. whether a storage benchmark
  touched a disk at all

Settings (``[host_probe]`` with ``type = "ssh"``)::

    host           = "db1.example.com"   # required
    user           = "root"
    port           = 22
    identity_file  = "~/.ssh/id_ed25519"
    cgroup         = "/sys/fs/cgroup/system.slice/pg-tenant.service"  # optional
    label          = "db1"               # node name recorded on host-vantage rows
    timeout_s      = 15

Everything is read in one ``ssh`` invocation: a probe that costs several round trips
would itself perturb the measurement window it is describing. Missing files are skipped
silently (not every node runs ZFS or exposes PSI); a probe that produces *no* readings at
all fails loudly instead.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
from collections.abc import Mapping

from .base import HostProbe, HostProbeError, Reading

_SECTION = "@@"

# One shell script, one round trip. Each section is guarded so a missing interface is a
# skipped section rather than a failed probe.
_SCRIPT = """
echo "{s}uname"; uname -r 2>/dev/null
echo "{s}nproc"; nproc 2>/dev/null
echo "{s}hostname"; hostname 2>/dev/null
echo "{s}osrelease"; cat /etc/os-release 2>/dev/null
echo "{s}loadavg"; cat /proc/loadavg 2>/dev/null
echo "{s}meminfo"; cat /proc/meminfo 2>/dev/null
echo "{s}pressure_cpu"; cat /proc/pressure/cpu 2>/dev/null
echo "{s}pressure_io"; cat /proc/pressure/io 2>/dev/null
echo "{s}pressure_memory"; cat /proc/pressure/memory 2>/dev/null
echo "{s}arcstats"; cat /proc/spl/kstat/zfs/arcstats 2>/dev/null
"""

_CGROUP_SCRIPT = """
echo "{s}cgroup_cpu"; cat {cgroup}/cpu.stat 2>/dev/null
echo "{s}cgroup_memory_current"; cat {cgroup}/memory.current 2>/dev/null
echo "{s}cgroup_memory_max"; cat {cgroup}/memory.max 2>/dev/null
"""


class SSHHostProbe(HostProbe):
    type = "ssh"

    def __init__(self, name: str, settings: Mapping[str, object]) -> None:
        super().__init__(name, settings)
        self.host = self._str("host", required=True)
        self.user = self._str("user", default="root")
        self.identity_file = self._str("identity_file", default="")
        self.cgroup = self._str("cgroup", default="")
        self._label = self._str("label", default="") or self.host

        port = self.settings.get("port", 22)
        if isinstance(port, bool) or not isinstance(port, int):
            raise HostProbeError("[host_probe]: 'port' must be an integer")
        self.port = port

        timeout = self.settings.get("timeout_s", 15)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise HostProbeError("[host_probe]: 'timeout_s' must be an integer")
        self.timeout_s = float(timeout)

    @property
    def label(self) -> str:
        return self._label

    def facts(self) -> dict[str, str]:
        sections = self._collect()
        facts: dict[str, str] = {}
        if kernel := sections.get("uname", "").strip():
            facts["kernel"] = kernel
        if nproc := sections.get("nproc", "").strip():
            facts["cpu_count"] = nproc
        if hostname := sections.get("hostname", "").strip():
            facts["hostname"] = hostname
        if pretty := _os_pretty_name(sections.get("osrelease", "")):
            facts["os"] = pretty
        mem = parse_meminfo(sections.get("meminfo", ""))
        if (total := mem.get("MemTotal")) is not None:
            facts["mem_total_bytes"] = str(int(total))
        if self.cgroup:
            facts["cgroup"] = self.cgroup
            if (limit := _parse_first_int(sections.get("cgroup_memory_max", ""))) is not None:
                facts["cgroup_memory_max_bytes"] = str(limit)
        facts["probe"] = f"ssh://{self.user}@{self.host}:{self.port}"
        return facts

    def sample(self) -> dict[str, Reading]:
        readings = readings_from_sections(self._collect())
        if not readings:
            raise HostProbeError(
                f"host probe {self.name!r} read no metrics from {self.host}; check that "
                f"/proc is readable and the 'cgroup' path (if set) exists"
            )
        return readings

    # -- transport ---------------------------------------------------------------------

    def _script(self) -> str:
        script = _SCRIPT.format(s=_SECTION)
        if self.cgroup:
            script += _CGROUP_SCRIPT.format(s=_SECTION, cgroup=shlex.quote(self.cgroup))
        return script

    def _collect(self) -> dict[str, str]:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self.timeout_s)}",
            "-p",
            str(self.port),
        ]
        if self.identity_file:
            cmd += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        cmd += [f"{self.user}@{self.host}", "sh", "-s"]

        try:
            proc = subprocess.run(
                cmd,
                input=self._script(),
                capture_output=True,
                text=True,
                timeout=self.timeout_s * 2,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HostProbeError("ssh executable not found; the ssh host probe needs it") from exc
        except subprocess.TimeoutExpired as exc:
            raise HostProbeError(f"host probe ssh to {self.host} timed out") from exc

        if proc.returncode != 0:
            raise HostProbeError(
                f"host probe ssh to {self.host} failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()[:300]}"
            )
        return parse_sections(proc.stdout)

    def _str(self, key: str, *, default: str = "", required: bool = False) -> str:
        val = self.settings.get(key, default)
        if not isinstance(val, str):
            raise HostProbeError(f"[host_probe]: {key!r} must be a string")
        if required and not val:
            raise HostProbeError(f"[host_probe]: {key!r} is required")
        return val


# -- parsing (pure functions - unit tested against real /proc output) ---------------------


def parse_sections(output: str) -> dict[str, str]:
    """Split the probe script's marker-delimited output into ``{section: body}``."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(_SECTION):
            if current is not None:
                sections[current] = "\n".join(lines)
            current = line[len(_SECTION) :].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines)
    return sections


def parse_meminfo(text: str) -> dict[str, float]:
    """``/proc/meminfo`` into bytes, keyed by field name."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = float(parts[0])
        except ValueError:
            continue
        if len(parts) > 1 and parts[1] == "kB":
            value *= 1024.0
        out[key.strip()] = value
    return out


def parse_pressure(text: str) -> dict[str, float]:
    """``/proc/pressure/*`` stall totals in microseconds, keyed ``some``/``full``."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        for field in fields[1:]:
            name, _, raw = field.partition("=")
            if name == "total":
                with contextlib.suppress(ValueError):
                    out[kind] = float(raw)
    return out


def parse_keyed_values(text: str) -> dict[str, float]:
    """``key value`` lines (cgroup ``cpu.stat``) into floats."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            out[fields[0]] = float(fields[1])
        except ValueError:
            continue
    return out


def parse_arcstats(text: str) -> dict[str, float]:
    """ZFS ``arcstats`` (``name type value`` columns) into floats."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            out[fields[0]] = float(fields[2])
        except ValueError:
            continue
    return out


def readings_from_sections(sections: Mapping[str, str]) -> dict[str, Reading]:
    """Assemble every reading the probe managed to collect."""
    readings: dict[str, Reading] = {}

    load = sections.get("loadavg", "").split()
    if load:
        with contextlib.suppress(ValueError):
            readings["load1"] = Reading(float(load[0]), "load")

    mem = parse_meminfo(sections.get("meminfo", ""))
    if (available := mem.get("MemAvailable")) is not None:
        readings["mem_available_bytes"] = Reading(available, "bytes")

    for kind in ("cpu", "io", "memory"):
        pressure = parse_pressure(sections.get(f"pressure_{kind}", ""))
        for scope, total in pressure.items():
            readings[f"psi_{kind}_{scope}_us"] = Reading(total, "us", counter=True)

    cpu = parse_keyed_values(sections.get("cgroup_cpu", ""))
    for key, unit in (
        ("usage_usec", "us"),
        ("throttled_usec", "us"),
        ("nr_throttled", "count"),
        ("nr_periods", "count"),
    ):
        if (value := cpu.get(key)) is not None:
            readings[f"cgroup_cpu_{key}"] = Reading(value, unit, counter=True)

    if (current := _parse_first_int(sections.get("cgroup_memory_current", ""))) is not None:
        readings["cgroup_memory_current_bytes"] = Reading(float(current), "bytes")

    arc = parse_arcstats(sections.get("arcstats", ""))
    for key, name, unit, counter in (
        ("hits", "zfs_arc_hits", "count", True),
        ("misses", "zfs_arc_misses", "count", True),
        ("size", "zfs_arc_size_bytes", "bytes", False),
    ):
        if (value := arc.get(key)) is not None:
            readings[name] = Reading(value, unit, counter=counter)

    return readings


def _os_pretty_name(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.partition("=")[2].strip().strip('"')
    return ""


def _parse_first_int(text: str) -> int | None:
    stripped = text.strip().split("\n")[0].strip() if text.strip() else ""
    try:
        return int(stripped)
    except ValueError:
        return None
