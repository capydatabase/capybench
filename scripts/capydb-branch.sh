#!/usr/bin/env bash
# Branch (preview-database) lifecycle helper for capybench's branch_speed scenario.
#
# The harness drives the real CapyDB API here rather than embedding a client. Emits ONE
# JSON object of connection fields on stdout (host/port/user/password/dbname/sslmode),
# progress on stderr — the shape capybench.control.commands.branch_target_from_json wants.
#
#   capydb-branch.sh create <projectID> <name>   # mode=clone (ZFS CoW), waits for creds
#   capydb-branch.sh delete <projectID> <name>
#
# Env: CAPYDB_API (default https://api.capydb.dev), CAPYDB_TOKEN (admin/org bearer).
set -euo pipefail

API="${CAPYDB_API:-https://api.capydb.dev}"
TOKEN="${CAPYDB_TOKEN:?CAPYDB_TOKEN must be set}"
ACTION="${1:?action required: create|delete}"
PROJECT="${2:?projectID required}"
NAME="${3:?branch name required}"

auth=(-H "Authorization: Bearer $TOKEN")

_py() { python3 -c "$1" "${@:2}"; }

preview_id_by_name() {
  curl -sS -m 30 "${auth[@]}" "$API/v1/projects/$PROJECT/preview-databases" \
    | _py 'import sys,json; n=sys.argv[1]; ps=json.load(sys.stdin).get("preview_databases",[]); print(next((p["id"] for p in ps if p.get("name")==n), ""))' "$NAME"
}

case "$ACTION" in
  create)
    echo "creating clone preview $NAME on $PROJECT" >&2
    curl -sS -m 60 -X POST "${auth[@]}" -H "Content-Type: application/json" \
      "$API/v1/projects/$PROJECT/preview-databases" \
      -d "{\"name\":\"$NAME\",\"mode\":\"clone\",\"ttl_hours\":2}" \
      | _py 'import sys,json; d=json.load(sys.stdin); p=d.get("preview") or {}; print(p.get("id") or d.get("error") or "no-id", file=sys.stderr)' >/dev/null || true

    pid="$(preview_id_by_name)"
    if [[ -z "$pid" ]]; then echo "preview $NAME not found after create" >&2; exit 1; fi
    echo "preview id $pid; polling for connectable credentials" >&2

    # Poll the connections endpoint until the preview instance is up and creds resolve.
    deadline=$(( SECONDS + 180 ))
    while (( SECONDS < deadline )); do
      body="$(curl -sS -m 20 "${auth[@]}" "$API/v1/preview-databases/$pid/connections" || true)"
      json="$(printf '%s' "$body" | _py '
import sys,json
from urllib.parse import urlparse
try:
    d=json.load(sys.stdin).get("connections") or {}
except Exception:
    sys.exit(1)
u=d.get("direct_url")
if not u: sys.exit(1)
p=urlparse(u)
print(json.dumps({"host":p.hostname,"port":p.port,"user":p.username,"password":p.password,"dbname":p.path.lstrip("/").split("?")[0],"sslmode":"require","preview_id":sys.argv[1]}))
' "$pid" 2>/dev/null || true)"
      if [[ -n "$json" ]]; then echo "$json"; exit 0; fi
      sleep 2
    done
    echo "preview $NAME never became connectable" >&2
    exit 1
    ;;

  delete)
    pid="$(preview_id_by_name)"
    if [[ -z "$pid" ]]; then echo "{}"; exit 0; fi
    curl -sS -m 60 -X DELETE "${auth[@]}" "$API/v1/preview-databases/$pid" >/dev/null || true
    echo "{\"deleted\":\"$pid\"}"
    ;;

  *)
    echo "unknown action $ACTION" >&2; exit 2;;
esac
