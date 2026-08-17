"""CapyDB provider: provisioning, preview-database branches, forced sleep, PITR restore.

Talks to the CapyDB control plane directly (stdlib ``urllib``, no extra dependencies).
The API token is read from an environment variable - never from the TOML file - so
configs stay committable.

Settings (``[providers.<name>]`` with ``type = "capydb"``)::

    api_url          = "https://api.capydb.dev"   # control-plane base URL
    token_env        = "CAPYDB_TOKEN"             # env var holding the bearer token
    branch_ttl_hours = 2                          # TTL for created preview databases
    branch_timeout_s = 180                        # poll budget for branch credentials
    sleep_timeout_s  = 60                         # wait budget for the sleep job
    provision_timeout_s = 300                     # wait budget for project create/destroy
    restore_timeout_s   = 900                     # wait budget for the restore job
    organization_id  = "org_..."                  # only for platform admins acting for an org

Targets using this provider must set ``project`` to the CapyDB project ID. Branching and
provisioning need an org-scoped API key; ``trigger_sleep`` uses an admin endpoint and
therefore a platform-admin token (i.e. it is intended for CapyDB operators benchmarking
their own platform, not for customers).

``restore`` always restores into a *new preview database*, never over the source project.
The control plane does expose a production-overwrite restore; it is deliberately not
reachable from here.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib import error, parse, request

from ..capabilities import Capability
from ..config import Target
from .base import Provider, ProviderError

_DEFAULT_API_URL = "https://api.capydb.dev"
_POLL_INTERVAL_S = 2.0


class CapyDBProvider(Provider):
    type = "capydb"
    capabilities = frozenset(
        {
            Capability.PROVISION,
            Capability.BRANCH,
            Capability.SLEEP,
            Capability.RESTORE,
        }
    )

    def __init__(self, name: str, settings: Mapping[str, object]) -> None:
        super().__init__(name, settings)
        self.api_url = self._str_setting("api_url", _DEFAULT_API_URL).rstrip("/")
        self.token_env = self._str_setting("token_env", "CAPYDB_TOKEN")
        self.branch_ttl_hours = self._int_setting("branch_ttl_hours", 2)
        self.branch_timeout_s = float(self._int_setting("branch_timeout_s", 180))
        self.sleep_timeout_s = float(self._int_setting("sleep_timeout_s", 60))
        self.provision_timeout_s = float(self._int_setting("provision_timeout_s", 300))
        self.restore_timeout_s = float(self._int_setting("restore_timeout_s", 900))
        organization_id = self.settings.get("organization_id")
        if organization_id is not None and not isinstance(organization_id, str):
            raise ProviderError(f"[providers.{self.name}]: 'organization_id' must be a string")
        self.organization_id = organization_id

    # -- provisioning ------------------------------------------------------------------

    def provision(
        self,
        name: str,
        *,
        pg_version: str | None = None,
        region: str | None = None,
    ) -> Target:
        body: dict[str, Any] = {"name": name, "environment": "non_production"}
        if pg_version:
            body["postgres_version"] = pg_version
        if region:
            body["region"] = region
        if self.organization_id:
            body["organization_id"] = self.organization_id

        created = self._request("POST", "/v1/projects", body, timeout=60.0)
        project = created.get("project")
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, str) or not project_id:
            raise ProviderError(f"project create for {name!r} returned no project id")

        url = self._await_connection_url(
            f"/v1/projects/{project_id}/connections",
            what=f"project {name!r} ({project_id})",
            timeout_s=self.provision_timeout_s,
        )
        target = target_from_url(name, url)
        # Carry the platform identifier so destroy/sleep/restore can act on it later.
        return _with_project(target, project_id, provider=self.name, region=region)

    def destroy(self, target: Target) -> None:
        project = self._require_project(target)
        resp = self._request("DELETE", f"/v1/projects/{project}", timeout=60.0)
        job_id = _job_id(resp)
        if job_id is not None:
            self._wait_job(job_id, self.provision_timeout_s, what=f"delete of project {project}")

    # -- branching ---------------------------------------------------------------------

    def create_branch(self, parent: Target, branch_name: str) -> Target:
        project = self._require_project(parent)

        create_err: ProviderError | None = None
        try:
            self._request(
                "POST",
                f"/v1/projects/{project}/preview-databases",
                {"name": branch_name, "mode": "clone", "ttl_hours": self.branch_ttl_hours},
                timeout=60.0,
            )
        except ProviderError as exc:
            # The preview may already exist from an interrupted run; the by-name lookup
            # below is authoritative. If it finds nothing, the create error is re-raised.
            create_err = exc

        preview_id = self._preview_id_by_name(project, branch_name)
        if preview_id is None:
            if create_err is not None:
                raise create_err
            raise ProviderError(
                f"preview {branch_name!r} not found after create on project {project}"
            )
        return self._preview_target(parent, branch_name, preview_id, self.branch_timeout_s)

    def delete_branch(self, parent: Target, branch_name: str) -> None:
        project = self._require_project(parent)
        preview_id = self._preview_id_by_name(project, branch_name)
        if preview_id is None:
            return  # already gone; TTL cleanup may have raced us
        self._request("DELETE", f"/v1/preview-databases/{preview_id}", timeout=60.0)

    # -- scale-to-zero -----------------------------------------------------------------

    def trigger_sleep(self, target: Target) -> None:
        project = self._require_project(target)

        proj = self._request("GET", f"/v1/projects/{project}")
        project_obj = proj.get("project")
        instance = project_obj.get("primary_instance_id") if isinstance(project_obj, dict) else None
        if not isinstance(instance, str) or not instance:
            raise ProviderError(f"project {project} has no primary instance to sleep")

        job_resp = self._request("POST", f"/v1/admin/instances/{instance}/sleep", {"force": True})
        job_id = _job_id(job_resp)
        if job_id is None:
            raise ProviderError(f"sleep request for instance {instance} returned no job id")
        self._wait_job(job_id, self.sleep_timeout_s, what=f"sleep of instance {instance}")

    # -- restore -----------------------------------------------------------------------

    def restore(self, target: Target, name: str, *, restore_time: str | None = None) -> Target:
        project = self._require_project(target)
        # The API requires exactly one recovery target; "most recent recoverable state"
        # maps to a PITR request for right now.
        when = restore_time or datetime.now(UTC).isoformat(timespec="seconds")

        job_resp = self._request(
            "POST",
            f"/v1/projects/{project}/restores",
            {
                "target_kind": "new_preview",
                "preview_name": name,
                "restore_time": when,
                "ttl_hours": self.branch_ttl_hours,
            },
            timeout=60.0,
        )
        job_id = _job_id(job_resp)
        if job_id is None:
            raise ProviderError(f"restore request for project {project} returned no job id")
        self._wait_job(job_id, self.restore_timeout_s, what=f"restore of project {project}")

        preview_id = self._preview_id_by_name(project, name)
        if preview_id is None:
            raise ProviderError(f"restore target {name!r} not found after the restore job")
        return self._preview_target(target, name, preview_id, self.restore_timeout_s)

    # -- internals ---------------------------------------------------------------------

    def _preview_target(
        self, parent: Target, branch_name: str, preview_id: str, timeout_s: float
    ) -> Target:
        url = self._await_connection_url(
            f"/v1/preview-databases/{preview_id}/connections",
            what=f"branch {branch_name!r} (preview {preview_id})",
            timeout_s=timeout_s,
        )
        return branch_target_from_url(parent, branch_name, url)

    def _await_connection_url(self, path: str, *, what: str, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            data = self._request("GET", path)
            connections = data.get("connections")
            url = connections.get("direct_url") if isinstance(connections, dict) else None
            if isinstance(url, str) and url:
                return url
            time.sleep(_POLL_INTERVAL_S)
        raise ProviderError(f"{what} returned no connection credentials within {timeout_s:.0f}s")

    def _wait_job(self, job_id: str, timeout_s: float, *, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = self._request("GET", f"/v1/jobs/{job_id}").get("job")
            state = job.get("state") if isinstance(job, dict) else None
            if state == "completed":
                return
            if state == "failed":
                detail = job.get("error") if isinstance(job, dict) else None
                raise ProviderError(f"job {job_id} ({what}) failed: {detail or 'no detail'}")
            time.sleep(_POLL_INTERVAL_S)
        raise ProviderError(f"job {job_id} ({what}) did not finish within {timeout_s:.0f}s")

    def _str_setting(self, key: str, default: str) -> str:
        val = self.settings.get(key, default)
        if not isinstance(val, str) or not val:
            raise ProviderError(f"[providers.{self.name}]: {key!r} must be a non-empty string")
        return val

    def _int_setting(self, key: str, default: int) -> int:
        val = self.settings.get(key, default)
        if isinstance(val, bool) or not isinstance(val, int):
            raise ProviderError(f"[providers.{self.name}]: {key!r} must be an integer")
        return val

    def _require_project(self, target: Target) -> str:
        if not target.project:
            raise ProviderError(
                f"target {target.name!r} needs 'project' set to a CapyDB project ID "
                f"for provider {self.name!r} lifecycle ops"
            )
        return target.project

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise ProviderError(
                f"provider {self.name!r}: set the {self.token_env} environment variable "
                f"to a CapyDB API token"
            )
        return token

    def _preview_id_by_name(self, project: str, branch_name: str) -> str | None:
        data = self._request("GET", f"/v1/projects/{project}/preview-databases")
        previews = data.get("preview_databases")
        if not isinstance(previews, list):
            raise ProviderError(f"unexpected preview-databases response for project {project}")
        for preview in previews:
            if isinstance(preview, dict) and preview.get("name") == branch_name:
                preview_id = preview.get("id")
                if isinstance(preview_id, str) and preview_id:
                    return preview_id
        return None

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token()}")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ProviderError(
                f"capydb API {method} {path} failed: HTTP {exc.code} {detail}"
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"capydb API {method} {path} unreachable: {reason}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"capydb API {method} {path} returned non-JSON") from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                f"capydb API {method} {path} returned {type(parsed).__name__}, expected object"
            )
        return parsed


def _job_id(response: dict[str, Any]) -> str | None:
    job = response.get("job")
    job_id = job.get("id") if isinstance(job, dict) else None
    return job_id if isinstance(job_id, str) and job_id else None


def _with_project(target: Target, project: str, *, provider: str, region: str | None) -> Target:
    return Target(
        name=target.name,
        host=target.host,
        port=target.port,
        user=target.user,
        dbname=target.dbname,
        password=target.password,
        sslmode=target.sslmode,
        region=region,
        provider=provider,
        project=project,
    )


def target_from_url(name: str, direct_url: str, *, template: Target | None = None) -> Target:
    """Map a ``postgres://`` connection URL onto a connectable Target.

    Anything the URL omits falls back to ``template`` (the parent target) when one is
    given, and to libpq-ish defaults otherwise.
    """
    parts = parse.urlparse(direct_url)
    if not parts.hostname:
        raise ProviderError(f"{name}: unparseable connection URL (no hostname)")
    dbname = parts.path.lstrip("/").split("?")[0]
    return Target(
        name=name,
        host=parts.hostname,
        port=parts.port or (template.port if template else 5432),
        user=parse.unquote(parts.username)
        if parts.username
        else (template.user if template else "postgres"),
        dbname=dbname or (template.dbname if template else "postgres"),
        password=parse.unquote(parts.password)
        if parts.password
        else (template.password if template else None),
        sslmode="require",
        tier_usd=template.tier_usd if template else None,
        pg_version=template.pg_version if template else None,
        region=template.region if template else None,
        provider=template.provider if template else "generic",
        project=template.project if template else None,
    )


def branch_target_from_url(parent: Target, branch_name: str, direct_url: str) -> Target:
    """Map a branch's connection URL onto a Target, inheriting the parent's metadata."""
    return target_from_url(f"{parent.name}:{branch_name}", direct_url, template=parent)
