"""Run-scoped naming for the databases lifecycle scenarios create.

Every branch, restore target and provisioned project is named after the run that made it.
Two reasons: a leftover from an interrupted run can never be mistaken for a fresh one (a
stale branch would report a fake instant creation), and anything left behind is
attributable to a specific run when cleaning up.
"""

from __future__ import annotations

_HEX = "0123456789abcdef"


def run_token(run_id: str) -> str:
    """Ten hex characters of a run id - unique per run and safe in a database name."""
    return "".join(char for char in run_id.lower() if char in _HEX)[:10] or "run"
