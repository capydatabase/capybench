#!/usr/bin/env bash
# Force-sleep a project's primary instance via the admin API (cold_start scenario).
# Emits {} on success; the wake side needs no command — the first client connection
# wakes the instance through the routing proxy (that connect IS the measurement).
#
#   capydb-sleep.sh <projectID>
#
# Env: CAPYDB_API (default https://api.capydb.dev), CAPYDB_TOKEN (platform-admin bearer).
set -euo pipefail

API="${CAPYDB_API:-https://api.capydb.dev}"
TOKEN="${CAPYDB_TOKEN:?CAPYDB_TOKEN must be set}"
PROJECT="${1:?projectID required}"

auth=(-H "Authorization: Bearer $TOKEN")

instance="$(curl -sS -m 30 "${auth[@]}" "$API/v1/projects/$PROJECT" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["project"]["primary_instance_id"])')"
[[ -n "$instance" ]] || { echo "no primary instance for $PROJECT" >&2; exit 1; }

job="$(curl -sS -m 30 -X POST "${auth[@]}" -H "Content-Type: application/json" \
  -d '{"force":true}' "$API/v1/admin/instances/$instance/sleep" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job"]["id"])')"
echo "sleep job $job for $instance" >&2

deadline=$(( SECONDS + 60 ))
while (( SECONDS < deadline )); do
  state="$(curl -sS -m 15 "${auth[@]}" "$API/v1/jobs/$job" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("job",{}).get("state",""))')"
  if [[ "$state" == "completed" ]]; then echo "{}"; exit 0; fi
  if [[ "$state" == "failed" ]]; then echo "sleep job $job failed" >&2; exit 1; fi
  sleep 2
done
echo "sleep job $job did not finish within 60s" >&2
exit 1
