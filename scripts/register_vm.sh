#!/usr/bin/env bash
# OpenCode VMware Model Pool - Manual VM Registration CLI
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -lt 2 ]; then
  echo "Usage: $0 <VM_URL> <REGISTRATION_TOKEN>"
  echo "Example: $0 http://192.168.1.150:4096 tok_xyz123..."
  exit 1
fi

VM_URL="$1"
TOKEN="$2"

python3 "$PROJECT_ROOT/host/installer.py" --vm-url "$VM_URL" --token "$TOKEN"

