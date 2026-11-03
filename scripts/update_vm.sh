#!/usr/bin/env bash
# OpenCode VMware Model Pool - Update VM Models CLI
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VM_ID="${1:-all}"

python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from host.registry import VMRegistry
from host.model_discovery import ModelDiscovery

reg = VMRegistry()
discovery = ModelDiscovery(registry=reg)

target = '$VM_ID'
if target == 'all':
    print('--> Refreshing models across all registered VMs...')
    res = discovery.refresh_all_vms()
    for vid, count in res.items():
        print(f'    VM {vid}: {count} model(s) synced')
else:
    print(f'--> Refreshing models for VM {target}...')
    try:
        synced = discovery.discover_and_sync_vm(target)
        print(f'    VM {target}: {len(synced)} model(s) synced')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

print('--> Host OpenCode configuration synchronized.')
"

