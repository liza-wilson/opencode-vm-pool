#!/usr/bin/env bash
# Start the OpenCode VM Pool Host Gateway (no systemd required).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/.config/opencode-vm-pool/gateway.log"

if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
  echo "Gateway already running at http://127.0.0.1:8080"
  exit 0
fi

mkdir -p "$(dirname "$LOG")"
nohup python3 "$ROOT/host/gateway.py" --host 127.0.0.1 --port 8080 >"$LOG" 2>&1 &
echo "Gateway started (PID $!), log: $LOG"

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
    echo "Gateway healthy at http://127.0.0.1:8080"
    exit 0
  fi
  sleep 0.2
done
echo "Gateway did not become healthy; check $LOG" >&2
exit 1
