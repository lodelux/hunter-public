#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${HUNTER_REMOTE_HOST:?Set HUNTER_REMOTE_HOST, for example user@hunter-host}"
REMOTE_PORT="${HUNTER_REMOTE_PORT:-22}"
REMOTE_DIR="${HUNTER_REMOTE_DIR:?Set HUNTER_REMOTE_DIR to the remote checkout path}"
WAIT_SECONDS="${HUNTER_DEPLOY_WAIT_SECONDS:-1800}"
TARGET_REF="${1:-origin/main}"

cd "$PROJECT_DIR"

git fetch --quiet origin main
TARGET_SHA="$(git rev-parse --verify "${TARGET_REF}^{commit}")"
ORIGIN_MAIN_SHA="$(git rev-parse --verify "origin/main^{commit}")"

if [[ "$TARGET_SHA" != "$ORIGIN_MAIN_SHA" ]]; then
  echo "Refusing to deploy $TARGET_SHA: deployments must use the current origin/main ($ORIGIN_MAIN_SHA)." >&2
  exit 1
fi

SSH_OPTIONS=(
  -p "$REMOTE_PORT"
  -o BatchMode=yes
  -o ConnectTimeout=10
)
printf -v HEAD_COMMAND 'git -C %q rev-parse HEAD' "$REMOTE_DIR"
REMOTE_HEAD="$(ssh "${SSH_OPTIONS[@]}" "$REMOTE_HOST" "$HEAD_COMMAND")"
REMOTE_BUNDLE="-"
TEMP_DIR="$(mktemp -d)"
WEB_ARCHIVE="$TEMP_DIR/hunter-web.tar.gz"
REMOTE_WEB_ARCHIVE="$REMOTE_DIR/.git/hunter-web-$TARGET_SHA.tar.gz"

cleanup_local_artifacts() {
  [[ ! -f "$TEMP_DIR/hunter.bundle" ]] || unlink "$TEMP_DIR/hunter.bundle"
  [[ ! -f "$WEB_ARCHIVE" ]] || unlink "$WEB_ARCHIVE"
  rmdir "$TEMP_DIR" 2>/dev/null || true
}
trap cleanup_local_artifacts EXIT

echo "Building hosted web frontend..."
npm run build
[[ -f dist/index.html ]] || {
  echo "Hosted web build did not create dist/index.html." >&2
  exit 1
}
tar -C dist -czf "$WEB_ARCHIVE" .
scp \
  -P "$REMOTE_PORT" \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  "$WEB_ARCHIVE" \
  "$REMOTE_HOST:$REMOTE_WEB_ARCHIVE"

if [[ "$REMOTE_HEAD" != "$TARGET_SHA" ]]; then
  if git cat-file -e "${REMOTE_HEAD}^{commit}" 2>/dev/null; then
    git bundle create "$TEMP_DIR/hunter.bundle" origin/main "^$REMOTE_HEAD"
  else
    git bundle create "$TEMP_DIR/hunter.bundle" origin/main
  fi
  REMOTE_BUNDLE="$REMOTE_DIR/.git/hunter-deploy-$TARGET_SHA.bundle"
  scp \
    -P "$REMOTE_PORT" \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    "$TEMP_DIR/hunter.bundle" \
    "$REMOTE_HOST:$REMOTE_BUNDLE"
fi

printf -v REMOTE_COMMAND 'bash -s -- %q %q %q %q %q' \
  "$REMOTE_DIR" "$TARGET_SHA" "$WAIT_SECONDS" "$REMOTE_BUNDLE" "$REMOTE_WEB_ARCHIVE"

ssh \
  "${SSH_OPTIONS[@]}" \
  "$REMOTE_HOST" \
  "$REMOTE_COMMAND" <<'REMOTE_SCRIPT'
set -euo pipefail

PROJECT_DIR="$1"
TARGET_SHA="$2"
WAIT_SECONDS="$3"
BUNDLE_PATH="$4"
WEB_ARCHIVE_PATH="$5"
BASE_URL="http://127.0.0.1:8743"
WEB_STAGE=""

fail() {
  echo "$1" >&2
  exit 1
}

cleanup_remote_artifacts() {
  if [[ "$BUNDLE_PATH" != "-" && -f "$BUNDLE_PATH" ]]; then
    unlink "$BUNDLE_PATH"
  fi
  if [[ -f "$WEB_ARCHIVE_PATH" ]]; then
    unlink "$WEB_ARCHIVE_PATH"
  fi
  if [[ -n "$WEB_STAGE" && -d "$WEB_STAGE" ]]; then
    find "$WEB_STAGE" -mindepth 1 -delete
    rmdir "$WEB_STAGE" 2>/dev/null || true
  fi
}
trap cleanup_remote_artifacts EXIT

read_token() {
  local token=""
  local token_entry=""
  if tmux has-session -t hunter-backend 2>/dev/null; then
    token_entry="$(tmux show-environment -t hunter-backend JOB_APPLICANT_TOKEN 2>/dev/null || true)"
    if [[ "$token_entry" == JOB_APPLICANT_TOKEN=* ]]; then
      token="${token_entry#JOB_APPLICANT_TOKEN=}"
    fi
  fi
  if [[ -z "$token" && -f "$HOME/.config/langhire/.api_token" ]]; then
    token="$(<"$HOME/.config/langhire/.api_token")"
  fi
  printf '%s' "$token"
}

api_get() {
  curl -fsS -H "Authorization: Bearer $API_TOKEN" "$BASE_URL$1"
}

api_post() {
  curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" "$BASE_URL$1"
}

automation_fields() {
  python3 -c '
import json, sys
payload = json.load(sys.stdin)
config = payload.get("config") or {}
status = payload.get("status") or {}
print(
    1 if config.get("enabled") else 0,
    status.get("state") or "unknown",
    status.get("phase") or "unknown",
    status.get("current_job_url") or "-",
    sep="\t",
)
'
}

cd "$PROJECT_DIR"

[[ "$(git symbolic-ref --short HEAD 2>/dev/null || true)" == "main" ]] \
  || fail "Remote Hunter checkout must be on main."

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  git status --short >&2
  fail "Remote Hunter checkout is dirty; refusing deployment."
fi

if [[ "$BUNDLE_PATH" != "-" ]]; then
  git bundle verify "$BUNDLE_PATH" >/dev/null
  git fetch --quiet "$BUNDLE_PATH" \
    refs/remotes/origin/main:refs/remotes/origin/main
fi
[[ "$(git rev-parse origin/main)" == "$TARGET_SHA" ]] \
  || fail "Remote origin/main does not match requested commit $TARGET_SHA."
git merge-base --is-ancestor HEAD "$TARGET_SHA" \
  || fail "Remote checkout cannot fast-forward to $TARGET_SHA."

tmux has-session -t hunter-backend 2>/dev/null \
  || fail "hunter-backend is not running; inspect the stopped runtime before deploying."
curl -fsS "$BASE_URL/health" >/dev/null \
  || fail "Hunter health check failed; inspect the runtime before deploying."

API_TOKEN="$(read_token)"
[[ -n "$API_TOKEN" ]] || fail "Hunter API token is unavailable."

automation_json="$(api_get /automation)"
IFS=$'\t' read -r was_enabled initial_state initial_phase initial_job \
  <<<"$(printf '%s' "$automation_json" | automation_fields)"
manual_running="$(api_get /apply/status | python3 -c 'import json, sys; print(1 if json.load(sys.stdin).get("running") else 0)')"
if [[ "$manual_running" == "1" && ( "$initial_phase" != "applying" || "$initial_job" == "-" ) ]]; then
  fail "A manual application is active; deployment was not started."
fi
manual_browser_running="$(
  api_get /browser/manual/status 2>/dev/null \
    | python3 -c 'import json, sys; print(1 if json.load(sys.stdin).get("state") in {"starting", "running", "stopping"} else 0)' 2>/dev/null \
    || printf '0'
)"
[[ "$manual_browser_running" == "0" ]] \
  || fail "The manual remote browser is active; stop it before deploying."

restore_autonomy=0
deadline=$((SECONDS + WAIT_SECONDS))
if [[ "$was_enabled" == "1" && "$initial_state" == "running" ]]; then
  restore_autonomy=1
  while [[ "$initial_phase" == "collecting" || "$initial_phase" == "classifying" ]]; do
    (( SECONDS < deadline )) \
      || fail "Timed out waiting for the active autonomous collection cycle to finish."
    echo "Waiting for the active autonomous collection cycle to finish..."
    sleep 10
    automation_json="$(api_get /automation)"
    IFS=$'\t' read -r _ initial_state initial_phase initial_job \
      <<<"$(printf '%s' "$automation_json" | automation_fields)"
  done
  echo "Stopping future autonomous work before deployment..."
  api_post /automation/stop >/dev/null
fi

while true; do
  automation_json="$(api_get /automation)"
  IFS=$'\t' read -r _ current_state current_phase current_job \
    <<<"$(printf '%s' "$automation_json" | automation_fields)"

  if [[ "$current_phase" != "applying" && "$current_job" == "-" ]]; then
    break
  fi
  if [[ "$current_phase" != "applying" ]]; then
    fail "Hunter has an unresolved current job ($current_job); deployment was not started."
  fi
  (( SECONDS < deadline )) \
    || fail "Timed out waiting for the active autonomous application to finish."
  echo "Waiting for the active autonomous application to finish..."
  sleep 10
done

manual_running="$(api_get /apply/status | python3 -c 'import json, sys; print(1 if json.load(sys.stdin).get("running") else 0)')"
[[ "$manual_running" == "0" ]] \
  || fail "A manual application started during deployment preparation; deployment was not started."
manual_browser_running="$(
  api_get /browser/manual/status 2>/dev/null \
    | python3 -c 'import json, sys; print(1 if json.load(sys.stdin).get("state") in {"starting", "running", "stopping"} else 0)' 2>/dev/null \
    || printf '0'
)"
[[ "$manual_browser_running" == "0" ]] \
  || fail "The manual remote browser started during deployment preparation; deployment was not started."

OLD_SHA="$(git rev-parse HEAD)"
changed_files="$(git diff --name-only "$OLD_SHA" "$TARGET_SHA")"
git merge --ff-only "$TARGET_SHA"

if grep -Eq '^(pyproject\.toml|uv\.lock)$' <<<"$changed_files"; then
  UV_BIN="$(command -v uv || true)"
  [[ -n "$UV_BIN" ]] || UV_BIN="$HOME/.local/bin/uv"
  [[ -x "$UV_BIN" ]] || fail "Dependencies changed, but uv is not installed on the remote."
  echo "Synchronizing changed Python dependencies..."
  "$UV_BIN" sync
fi

WEB_ROOT="$HOME/.config/langhire/web"
WEB_RELEASE="$WEB_ROOT/releases/$TARGET_SHA"
mkdir -p "$WEB_ROOT/releases"
if [[ ! -d "$WEB_RELEASE" ]]; then
  WEB_STAGE="$(mktemp -d "$WEB_ROOT/.release-$TARGET_SHA.XXXXXX")"
  tar -xzf "$WEB_ARCHIVE_PATH" -C "$WEB_STAGE"
  [[ -f "$WEB_STAGE/index.html" ]] \
    || fail "Hosted web archive does not contain index.html."
  mv "$WEB_STAGE" "$WEB_RELEASE"
  WEB_STAGE=""
fi
WEB_LINK="$WEB_ROOT/.current-$TARGET_SHA"
[[ ! -e "$WEB_LINK" ]] || unlink "$WEB_LINK"
ln -s "releases/$TARGET_SHA" "$WEB_LINK"
mv -Tf "$WEB_LINK" "$WEB_ROOT/current"

[[ -z "$(git status --porcelain --untracked-files=normal)" ]] \
  || fail "Remote checkout became dirty after the fast-forward."

echo "Restarting hunter-backend..."
tmux kill-session -t hunter-backend
for _ in {1..20}; do
  if ! curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

tmux new-session -d -s hunter-backend "cd '$PROJECT_DIR' && exec scripts/run-hunter-remote.sh"

healthy=0
for _ in {1..30}; do
  if curl -fsS "$BASE_URL/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
[[ "$healthy" == "1" ]] || fail "Hunter did not become healthy after restart."

API_TOKEN="$(read_token)"
[[ -n "$API_TOKEN" ]] || fail "Hunter restarted, but its API token is unavailable."

if [[ "$restore_autonomy" == "1" ]]; then
  echo "Restoring autonomous mode..."
  api_post /automation/start >/dev/null
fi

automation_json="$(api_get /automation)"
IFS=$'\t' read -r final_enabled final_state final_phase final_job \
  <<<"$(printf '%s' "$automation_json" | automation_fields)"

[[ "$(git rev-parse HEAD)" == "$TARGET_SHA" ]] \
  || fail "Remote checkout is not at the requested commit after deployment."
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] \
  || fail "Remote checkout is dirty after deployment."

echo "Deployed $TARGET_SHA"
echo "Hunter is healthy; autonomy enabled=$final_enabled state=$final_state phase=$final_phase current_job=$final_job"
REMOTE_SCRIPT
