#!/usr/bin/env bash
set -euo pipefail

remote_host="${HUNTER_REMOTE_HOST:?Set HUNTER_REMOTE_HOST, for example user@hunter-host}"
ssh_port="${HUNTER_REMOTE_PORT:-22}"
remote_vnc_port="${HUNTER_REMOTE_VNC_PORT:-5901}"
local_vnc_port="${HUNTER_LOCAL_VNC_PORT:-5901}"

for value in "$ssh_port" "$remote_vnc_port" "$local_vnc_port"; do
  if [[ ! "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > 65535 )); then
    echo "Invalid port: $value" >&2
    exit 1
  fi
done

if ! command -v ssh >/dev/null 2>&1 || ! command -v open >/dev/null 2>&1; then
  echo "This launcher requires macOS with ssh and open available." >&2
  exit 1
fi

if command -v nc >/dev/null 2>&1 && nc -z 127.0.0.1 "$local_vnc_port" >/dev/null 2>&1; then
  echo "Local port $local_vnc_port is already in use." >&2
  echo "Choose another with HUNTER_LOCAL_VNC_PORT, for example 5902." >&2
  exit 1
fi

if ! ssh -p "$ssh_port" -o BatchMode=yes -o ConnectTimeout=8 "$remote_host" \
  "ss -ltnH | awk '{print \$4}' | grep -Eq '(^|:)$remote_vnc_port$'"; then
  echo "Hunter's remote browser is not listening on port $remote_vnc_port." >&2
  echo "Open Hunter Settings and click Start browser first." >&2
  exit 1
fi

vnc_password="$(ssh -p "$ssh_port" -o BatchMode=yes -o ConnectTimeout=8 \
  "$remote_host" "cat ~/.config/langhire/manual_browser.password")"
if [[ ${#vnc_password} -ne 8 ]]; then
  echo "Could not read Hunter's one-session VNC password." >&2
  exit 1
fi
printf '%s' "$vnc_password" | pbcopy
unset vnc_password

ssh -p "$ssh_port" \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -N \
  -L "127.0.0.1:${local_vnc_port}:127.0.0.1:${remote_vnc_port}" \
  "$remote_host" &
tunnel_pid=$!

cleanup() {
  if kill -0 "$tunnel_pid" >/dev/null 2>&1; then
    kill "$tunnel_pid" >/dev/null 2>&1 || true
    wait "$tunnel_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

for _ in {1..40}; do
  if ! kill -0 "$tunnel_pid" >/dev/null 2>&1; then
    wait "$tunnel_pid"
    exit $?
  fi
  if ! command -v nc >/dev/null 2>&1 || nc -z 127.0.0.1 "$local_vnc_port" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

echo "Opening Hunter's remote browser. The one-session VNC password is in your clipboard."
echo "Paste it into Screen Sharing when prompted. Keep this terminal open; press Ctrl-C to disconnect."
open "vnc://127.0.0.1:${local_vnc_port}"
wait "$tunnel_pid"
