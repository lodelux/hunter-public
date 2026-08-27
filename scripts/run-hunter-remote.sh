#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${JOB_APPLICANT_TOKEN:-}" && -f "$HOME/.config/langhire/.api_token" ]]; then
  JOB_APPLICANT_TOKEN="$(<"$HOME/.config/langhire/.api_token")"
  export JOB_APPLICANT_TOKEN
fi

# Tailscale Serve forwards verified identity headers from localhost. Derive the
# owning login and private HTTPS origin so only that owner can use hosted web.
if command -v tailscale >/dev/null 2>&1; then
  mapfile -t tailscale_web_identity < <(
    tailscale status --json 2>/dev/null | python3 -c '
import json, sys

try:
    status = json.load(sys.stdin)
    node = status.get("Self") or {}
    dns_name = str(node.get("DNSName") or "").rstrip(".")
    user_id = str(node.get("UserID") or "")
    user = (status.get("User") or {}).get(user_id) or {}
    login = str(user.get("LoginName") or "").strip()
except Exception:
    raise SystemExit(0)

if login and dns_name.endswith(".ts.net"):
    print(login)
    print(f"https://{dns_name}")
'
  )
  if [[ "${#tailscale_web_identity[@]}" == "2" ]]; then
    export HUNTER_TAILSCALE_USER="${HUNTER_TAILSCALE_USER:-${tailscale_web_identity[0]}}"
    export HUNTER_WEB_ORIGIN="${HUNTER_WEB_ORIGIN:-${tailscale_web_identity[1]}}"
    export HUNTER_WEB_ENABLED="${HUNTER_WEB_ENABLED:-true}"
  fi
fi

exec .venv/bin/python scripts/hunter_watchdog.py
