#!/usr/bin/env bash
# OpenCode VMware Model Pool - Remove VM CLI
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <VM_ID>"
  echo "Example: $0 vm-a1b2c3d4"
  echo ""
  echo "Listing currently registered VMs:"
  python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from host.registry import VMRegistry
reg = VMRegistry()
for v in reg.list_vms():
    print(f'  - {v.vm_id:<16} {v.vm_name:<16} ({v.host}:{v.port}) [{v.status}]')
"
  exit 1
fi

VM_ID="$1"

python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from host.registry import VMRegistry
from host.model_discovery import ModelDiscovery

reg = VMRegistry()
discovery = ModelDiscovery(registry=reg)

if reg.unregister_vm('$VM_ID'):
    discovery.update_host_opencode_config()
    print(f'--> Successfully removed VM \"$VM_ID\" and cleaned up OpenCode models.')
else:
    print(f'--> VM \"$VM_ID\" was not found in registry.', file=sys.stderr)
    sys.exit(1)
"

