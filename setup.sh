#!/usr/bin/env bash
# OpenCode VMware Model Pool - Universal Setup Entrypoint
# Run this script immediately after cloning or extracting the zip archive.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure all sub-scripts have execution permissions
chmod +x scripts/*.sh vm/setup_opencode_vm.sh 2>/dev/null || true

# Display Banner
echo "=========================================================="
echo "    OpenCode VMware Model Pool - Universal Installer     "
echo "=========================================================="

# Check Python requirement
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3.11+ is required but not installed." >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "--> Detected Python: $PY_VER"

show_help() {
  echo "Usage: ./setup.sh [OPTION]"
  echo ""
  echo "Options:"
  echo "  --host        Install and start Host Gateway on this machine"
  echo "  --vm          Install and configure VM Agent inside a VMware VM"
  echo "  --test        Run the full automated test suite (25 tests)"
  echo "  --help, -h    Display this help message"
  echo ""
  echo "If no option is provided, an interactive prompt will be displayed."
}

MODE=""
for arg in "$@"; do
  case "$arg" in
    --host)
      MODE="host"
      ;;
    --vm)
      MODE="vm"
      ;;
    --test)
      MODE="test"
      ;;
    --help|-h)
      show_help
      exit 0
      ;;
    *)
      echo "Unknown option: $arg"
      show_help
      exit 1
      ;;
  esac
done

# If mode is not passed, prompt user if interactive, or auto-detect
if [ -z "$MODE" ]; then
  if [ -t 0 ]; then
    echo ""
    echo "Select target setup mode:"
    echo "  1) Host Machine    (Install Gateway, configure host OpenCode integration)"
    echo "  2) VMware Guest VM (Install VM Agent, configure OpenCode, output register command)"
    echo "  3) Run Test Suite  (Execute automated test suite)"
    echo ""
    read -r -p "Enter choice [1-3] (Default: 1): " CHOICE
    case "${CHOICE:-1}" in
      1) MODE="host" ;;
      2) MODE="vm" ;;
      3) MODE="test" ;;
      *)
        echo "Invalid selection. Defaulting to Host Machine."
        MODE="host"
        ;;
    esac
  else
    # Non-interactive fallback: detect virtualization
    if command -v systemd-detect-virt >/dev/null 2>&1 && [ "$(systemd-detect-virt)" = "vmware" ]; then
      MODE="vm"
    else
      MODE="host"
    fi
  fi
fi

echo ""
case "$MODE" in
  host)
    echo "=========================================================="
    echo " Setting up Host Machine..."
    echo "=========================================================="
    bash "$SCRIPT_DIR/scripts/install_host.sh"
    echo ""
    echo "--> Verifying Host Gateway daemon status..."
    python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from host.installer import ensure_host_gateway_running
if ensure_host_gateway_running(gateway_host='127.0.0.1', gateway_port=8080):
    print('--> Host Gateway is active at http://127.0.0.1:8080')
else:
    print('Notice: Gateway ready to run manually via: python3 host/gateway.py')
"
    echo ""
    echo "=========================================================="
    echo " Host Setup Complete!"
    echo " Ready to connect VMs. On each VM, run: ./setup.sh --vm"
    echo " Then run the generated curl command on this host."
    echo "=========================================================="
    ;;

  vm)
    echo "=========================================================="
    echo " Setting up VMware Linux VM Agent..."
    echo "=========================================================="
    bash "$SCRIPT_DIR/vm/setup_opencode_vm.sh"
    ;;

  test)
    echo "=========================================================="
    echo " Running Automated Test Suite..."
    echo "=========================================================="
    python3 -m unittest discover -s "$SCRIPT_DIR/tests" -v
    ;;
esac
