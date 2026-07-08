"""Execute provider lifecycle operations as configured shell commands.

We deliberately do **not** embed a CapyDB API client here. The supported, stable surface
for lifecycle ops is the ``capydb`` CLI (and each competitor's CLI); the harness drives
those via templates from ``[control.<provider>.commands]``. This keeps CapyBench from
duplicating — and drifting out of sync with — the control plane's real endpoints.

Each template must print a single JSON object to stdout. Branch-create output is expected
to expose connection fields so the harness can connect to the new branch; the exact keys
are provider-specific and mapped by :func:`branch_target_from_json`.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from ..config import ControlProvider, Target


class ControlError(RuntimeError):
    """A lifecycle command failed, was unconfigured, or returned unparseable output."""


def _run_template(template: str, variables: Mapping[str, str], *, op: str) -> dict[str, Any]:
    if not template.strip():
        raise ControlError(
            f"control op {op!r} is not configured; set control.<provider>.commands.{op} "
            f"to a command that performs it and prints JSON"
        )
    try:
        rendered = template.format(**variables)
    except KeyError as exc:
        raise ControlError(f"template for {op!r} references unknown variable {exc}") from exc

    proc = subprocess.run(
        shlex.split(rendered), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise ControlError(
            f"control op {op!r} failed (rc={proc.returncode}): {rendered}\n{proc.stderr}"
        )
    stdout = proc.stdout.strip()
    if not stdout:
        return {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ControlError(f"control op {op!r} did not emit JSON:\n{proc.stdout}") from exc
    if not isinstance(parsed, dict):
        raise ControlError(f"control op {op!r} JSON must be an object, got {type(parsed).__name__}")
    return parsed


def sleep_instance(control: ControlProvider, target: Target) -> None:
    _require_project(target)
    _run_template(control.sleep, {"project": target.project or ""}, op="sleep")


def wake_instance(control: ControlProvider, target: Target) -> None:
    _require_project(target)
    _run_template(control.wake, {"project": target.project or ""}, op="wake")


def create_branch(control: ControlProvider, parent: Target, branch_name: str) -> Target:
    """Create a branch and return a :class:`Target` connected to it."""
    _require_project(parent)
    result = _run_template(
        control.branch_create,
        {"project": parent.project or "", "branch": branch_name},
        op="branch_create",
    )
    return branch_target_from_json(parent, branch_name, result)


def delete_branch(control: ControlProvider, parent: Target, branch_name: str) -> None:
    if not control.branch_delete.strip():
        return  # deletion optional; skip cleanly if not configured
    _run_template(
        control.branch_delete,
        {"project": parent.project or "", "branch": branch_name},
        op="branch_delete",
    )


def branch_target_from_json(parent: Target, branch_name: str, data: Mapping[str, Any]) -> Target:
    """Map branch-create JSON onto a connectable Target.

    Falls back to the parent's connection fields for anything the command didn't return,
    so a provider that only changes the dbname (or nothing) still yields a usable target.
    """

    def pick(*keys: str, default: Any) -> Any:
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        return default

    return Target(
        name=f"{parent.name}:{branch_name}",
        host=str(pick("host", "hostname", default=parent.host)),
        port=int(pick("port", default=parent.port)),
        user=str(pick("user", "username", "role", default=parent.user)),
        dbname=str(pick("dbname", "database", default=parent.dbname)),
        password=pick("password", default=parent.password),
        sslmode=str(pick("sslmode", default=parent.sslmode)),
        tier_usd=parent.tier_usd,
        pg_version=parent.pg_version,
        region=parent.region,
        provider=parent.provider,
        project=str(pick("project", default=parent.project or "")),
    )


def wait_connectable(target: Target, *, timeout_s: float = 120.0, poll_s: float = 0.5) -> float:
    """Block until ``SELECT 1`` succeeds. Returns seconds waited.

    Used after branch creation to measure end-to-end "request → connectable" time.
    """
    import psycopg  # imported lazily so control has no hard psycopg dep at import time

    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            with psycopg.connect(target.dsn(), connect_timeout=5) as conn:
                conn.execute("select 1")
            return time.monotonic() - started
        except psycopg.Error as exc:
            last_err = exc
            time.sleep(poll_s)
    raise ControlError(f"branch {target.name} never became connectable: {last_err}")


def _require_project(target: Target) -> None:
    if not target.project:
        raise ControlError(
            f"target {target.name!r} needs a 'project' set for lifecycle control ops"
        )
