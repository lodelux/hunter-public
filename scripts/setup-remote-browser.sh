#!/usr/bin/env bash
set -euo pipefail

remote_host="${HUNTER_REMOTE_HOST:?Set HUNTER_REMOTE_HOST, for example user@hunter-host}"
ssh_port="${HUNTER_REMOTE_PORT:-22}"

if [[ ! "$ssh_port" =~ ^[0-9]+$ ]] || (( ssh_port < 1 || ssh_port > 65535 )); then
  echo "Invalid HUNTER_REMOTE_PORT: $ssh_port" >&2
  exit 1
fi

echo "Installing Xvfb and x11vnc on $remote_host..."
ssh -t -p "$ssh_port" "$remote_host" \
  'sudo apt-get update && sudo apt-get install -y xvfb x11vnc'

echo "Remote browser display dependencies are installed."
