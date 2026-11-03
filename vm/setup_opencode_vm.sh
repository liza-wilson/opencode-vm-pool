#!/usr/bin/env bash
# OpenCode VMware Model Pool - VM Setup Script
# Automatically provisions the VM Agent and OpenCode on Linux VMs
# Supports: Debian, Ubuntu, Kali, Fedora, Arch Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=========================================================="
echo " OpenCode VMware Model Pool - VM Agent Setup"
echo "=========================================================="

# 1. Detect Linux Distribution and Architecture
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)
    ARCH="x86_64"
    ;;
  aarch64|arm64)
    ARCH="aarch64"
    ;;
  *)
    echo "Warning: Architecture '$ARCH' may require custom binaries."
    ;;
esac

OS_ID="unknown"
OS_LIKE=""
if [ -f /etc/os-release ]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_LIKE="${ID_LIKE:-}"
fi

echo "--> Detected OS: $OS_ID ($OS_LIKE), Architecture: $ARCH"

# Determine privilege escalation
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "Warning: Not running as root and sudo is not available. System packages must be installed manually."
  fi
fi

# 2. Install Required Dependencies
echo "--> Checking and installing system dependencies..."
case "$OS_ID" in
  ubuntu|debian|kali|linuxmint|pop)
    if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
      $SUDO apt-get update -y -qq || true
      $SUDO apt-get install -y -qq python3 python3-pip curl iproute2 systemd jq || true
    fi
    ;;
  fedora|rhel|centos|rocky|alma)
    if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
      $SUDO dnf install -y python3 python3-pip curl iproute systemd jq || true
    fi
    ;;
  arch|manjaro|endeavouros)
    if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
      $SUDO pacman -Sy --noconfirm python python-pip curl iproute2 systemd jq || true
    fi
    ;;
  *)
    echo "--> Non-standard distribution ($OS_ID). Ensuring python3 and curl are available..."
    ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "Error: Python 3.11+ is required." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Error: curl is required." >&2; exit 1; }

# 3. Install OpenCode CLI if not present
echo "--> Checking OpenCode CLI installation..."
if ! command -v opencode >/dev/null 2>&1; then
  echo "--> Installing OpenCode CLI via official installer..."
  curl -fsSL https://opencode.ai/install | bash || {
    echo "Notice: OpenCode official installer completed or skipped. Standalone agent mode active."
  }
  # Export PATH for current session
  export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
fi

if command -v opencode >/dev/null 2>&1; then
  OC_VER="$(opencode --version 2>/dev/null || echo 'unknown')"
  echo "--> OpenCode CLI verified: $OC_VER"
else
  echo "--> OpenCode CLI will run in containerized or agent-managed mode."
fi

# 4. Generate Unique Credentials and Agent Configuration
echo "--> Initializing Agent configuration and credentials..."
CONFIG_DIR="/etc/opencode-vm-pool"
if [ "$(id -u)" -ne 0 ]; then
  CONFIG_DIR="$HOME/.config/opencode-vm-pool"
fi
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

CONFIG_PATH="$CONFIG_DIR/agent_config.json"

# Run Python helper to initialize config and generate registration token
TOKEN_JSON=$(python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from vm.config import VMConfig

cfg = VMConfig.load_or_create(path='$CONFIG_PATH')
tok = cfg.create_registration_token(ttl_seconds=900)
cfg.save('$CONFIG_PATH')

import json
print(json.dumps({
    'token': tok.token,
    'reachable_ip': cfg.reachable_ip,
    'port': cfg.port,
    'vm_name': cfg.vm_name,
    'vm_id': cfg.vm_id
}))
")

TOKEN=$(echo "$TOKEN_JSON" | grep -o '"token": *"[^"]*"' | cut -d'"' -f4)
VM_IP=$(echo "$TOKEN_JSON" | grep -o '"reachable_ip": *"[^"]*"' | cut -d'"' -f4)
PORT=$(echo "$TOKEN_JSON" | grep -o '"port": *[0-9]*' | grep -o '[0-9]*')
VM_NAME=$(echo "$TOKEN_JSON" | grep -o '"vm_name": *"[^"]*"' | cut -d'"' -f4)
VM_ID=$(echo "$TOKEN_JSON" | grep -o '"vm_id": *"[^"]*"' | cut -d'"' -f4)

# Open firewall port if ufw or iptables is present
if command -v ufw >/dev/null 2>&1; then
  $SUDO ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
fi
if command -v iptables >/dev/null 2>&1; then
  $SUDO iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || true
fi

# 5. Configure & Start Systemd Service
echo "--> Setting up systemd service for persistent agent..."
python3 -c "
import sys
sys.path.insert(0, '$PROJECT_ROOT')
from vm.systemd_manager import SystemdManager

mgr = SystemdManager()
if mgr.is_systemd_available():
    python_bin = sys.executable
    script_bin = '$SCRIPT_DIR/agent.py'
    cfg_path = '$CONFIG_PATH'
    if mgr.install_unit(python_bin, script_bin, cfg_path):
        mgr.enable_and_start()
        mgr.restart()
        print('--> systemd service enabled and restarted with latest token.')
    else:
        print('Notice: systemd service registration skipped.')
else:
    print('Notice: systemd not active in this environment. Background runner available.')
"

# 6. Verify Server Health & Discovered Models
echo "--> Verifying agent HTTP server is active and listening on port $PORT..."
HEALTH_OK=false
for i in $(seq 1 10); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    HEALTH_OK=true
    break
  fi
  sleep 0.5
done

if [ "$HEALTH_OK" = false ]; then
  echo "Notice: systemd service did not respond on port $PORT. Checking logs..."
  $SUDO journalctl -u opencode-vm-agent.service -n 10 --no-pager 2>/dev/null || true
  echo "--> Stopping any stale background agent..."
  pkill -f "agent.py" 2>/dev/null || true
  sleep 0.5
  echo "--> Starting agent directly in background..."
  nohup python3 "$SCRIPT_DIR/agent.py" --config "$CONFIG_PATH" --port "$PORT" > "$CONFIG_DIR/agent.log" 2>&1 &
  sleep 1
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    echo "--> Agent is now running in background."
    HEALTH_OK=true
  else
    echo "Warning: Agent is not responding on 127.0.0.1:$PORT. Check $CONFIG_DIR/agent.log."
  fi
else
  echo "--> Agent verified running and healthy on http://127.0.0.1:$PORT/health"
fi

# Verify registration token locally before displaying command
echo "--> Verifying registration token endpoint..."
if ! curl -s -m 2 "http://127.0.0.1:$PORT/register/$TOKEN" 2>/dev/null | grep -q "OpenCode VM Pool"; then
  echo "Notice: Refreshing token state on running agent..."
  pkill -f "agent.py" 2>/dev/null || true
  nohup python3 "$SCRIPT_DIR/agent.py" --config "$CONFIG_PATH" --port "$PORT" > "$CONFIG_DIR/agent.log" 2>&1 &
  sleep 1
fi

python3 -c "
import sys, json
sys.path.insert(0, '$PROJECT_ROOT')
from vm.opencode_manager import OpenCodeManager

mgr = OpenCodeManager()
models = mgr.discover_models()
print(f'--> Discovered {len(models)} model(s):')
for m in models:
    print(f'    - {m[\"id\"]} ({m[\"name\"]})')
"

REG_CMD="curl -fsSL \"http://${VM_IP}:${PORT}/register/${TOKEN}\" | bash"

echo ""
echo "=========================================================="
echo " OPENCODE VM SETUP COMPLETE!"
echo "=========================================================="
echo " VM Identifier: $VM_NAME ($VM_ID)"
echo " Reachable IP:  $VM_IP:$PORT"
echo " Token TTL:     15 minutes"
echo ""
echo " Run this command ON YOUR HOST MACHINE to register this VM:"
echo ""
echo "   $REG_CMD"
echo ""
echo "=========================================================="

