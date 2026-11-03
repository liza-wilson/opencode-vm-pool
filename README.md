# OpenCode VMware Model Pool

Production-ready infrastructure connecting a Linux host to multiple VMware Linux virtual machines hosting [OpenCode](https://github.com/anomalyco/opencode) model environments through a unified, authenticated, self-healing gateway.

---

## Table of Contents
1. [Overview & Architecture](#1-overview--architecture)
2. [OpenCode Integration Design Decision](#2-opencode-integration-design-decision)
3. [Supported Linux Distributions](#3-supported-linux-distributions)
4. [Prerequisites & Networking](#4-prerequisites--networking)
5. [User Workflow](#5-user-workflow)
   - [Inside VMware Linux VM](#inside-vmware-linux-vm)
   - [On the Host Machine](#on-the-host-machine)
   - [Adding Multiple VMs](#adding-multiple-vms)
6. [Model Routing & Automated Failover](#6-model-routing--automated-failover)
7. [Security Architecture](#7-security-architecture)
8. [CLI Reference & Management](#8-cli-reference--management)
9. [Troubleshooting Guide](#9-troubleshooting-guide)
10. [Automated Testing Suite](#10-automated-testing-suite)

---

## 1. Overview & Architecture

The OpenCode VMware Model Pool aggregates AI models configured across multiple VMware guest virtual machines (e.g., development VMs with separate API keys, corporate proxy configs, Copilot credentials, or local accelerators) and exposes them to the host's OpenCode client through a unified, high-availability gateway.

```
                  +----------------------------------------------+
                  |                 HOST MACHINE                 |
                  |                                              |
                  |   OpenCode CLI / TUI / Desktop               |
                  |   (~/.config/opencode/opencode.json)         |
                  |                      |                       |
                  |                      v (OpenAI-Compatible)   |
                  |          +-----------------------+           |
                  |          |      Host Gateway     |           |
                  |          |    (127.0.0.1:8080)   |           |
                  |          +-----------+-----------+           |
                  |                      |                       |
                  |        +-------------+-------------+         |
                  |        |                           |         |
                  |        v                           v         |
                  |   SQLite Registry           Health Monitor   |
                  |  (Active VMs & Models)     (Auto Recovery)   |
                  +--------+---------------------------+---------+
                           |                           |
        Network Boundary   | HTTP (Bearer Auth)        | HTTP (Bearer Auth)
                           v                           v
              +-------------------------+ +-------------------------+
              |     VM 1 (Bridged)      | |     VM 2 (Bridged)      |
              |     192.168.1.150:4096   | |     192.168.1.151:4096   |
              | +---------------------+ | | +---------------------+ |
              | |     VM Agent        | | | |     VM Agent        | |
              | | (systemd daemon)    | | | | (systemd daemon)    | |
              | +----------+----------+ | | +----------+----------+ |
              |            |            | |            |            |
              |            v            | |            v            |
              |   OpenCode Daemon       | |   OpenCode Daemon       |
              |  (Anthropic / Copilot)  | |  (OpenAI / Gemini)      |
              +-------------------------+ +-------------------------+
```

### Primary Components

1. **VM Agent (`vm/`)**:
   - Python 3.11+ daemon running as a persistent `systemd` service.
   - Automatically installs and verifies OpenCode.
   - Generates cryptographically secure API credentials and time-limited, single-use host registration tokens.
   - Discovers available models from OpenCode CLI, `auth.json`, and local config.
   - Exposes authenticated `/health`, `/models`, and `/v1/chat/completions` endpoints.
   - Detects reachable IPv4 interface (VMware Bridged / NAT).

2. **Host Gateway (`host/`)**:
   - Python 3.11+ async HTTP server exposing an OpenAI-compatible API (`/v1/models`, `/v1/chat/completions`).
   - SQLite-backed persistent registry (`registry.db`) tracking VM health, model mappings, and rate-limit states.
   - Intelligent request router with HTTP 429 rate-limit detection and automatic failover across candidate VMs.
   - Background health monitor tracking reachability, clearing cooldowns, and resyncing models on reconnect.
   - Zero-manual-edit OpenCode integration updating `~/.config/opencode/opencode.json`.

---

## 2. OpenCode Integration Design Decision

OpenCode provides three potential integration mechanisms. Each was evaluated against multi-VM pooling requirements:

| Mechanism | Description | Multi-VM Aggregation | Failover / 429 Retry | Patching Required | Evaluation |
|---|---|---|---|---|---|
| **Native Remote Client (`opencode attach`)** | Attaches terminal TUI to a single remote OpenCode daemon | ❌ Single VM only | ❌ None | ❌ None | Rejected: Designed for remote interactive sessions on one machine; cannot aggregate or failover between multiple VMs. |
| **Custom Hono Route Gateway** | Modifies OpenCode server internal session routes | ⚠️ Fragile | ⚠️ Unstable API | ⚠️ Fork / Patch required | Rejected: OpenCode server API routes (`/session`, `/api`) are tightly coupled to workspace state and change across minor versions. |
| **OpenAI-Compatible Custom Provider (`@ai-sdk/openai-compatible`)** | Configured as custom provider in `opencode.json` pointing to Host Gateway | ✅ Full support | ✅ Transparent | ❌ Zero patching | **Selected & Implemented**: Supported officially by OpenCode's configuration engine. Models appear natively in `/models` picker; completions stream with full SSE support; failover is 100% transparent. |

**Decision Summary**: The Host Gateway exposes an OpenAI-compatible endpoint (`/v1`) that is registered into OpenCode's global configuration (`opencode.json`) using `@ai-sdk/openai-compatible`. This guarantees long-term stability across OpenCode releases without modifying upstream binaries.

---

## 3. Supported Linux Distributions

The VM Agent and setup script support:

| Distribution Family | Tested Distributions | Package Manager | Init System |
|---|---|---|---|
| **Debian-based** | Debian 11/12, Ubuntu 22.04/24.04/26.04, Kali Linux, Pop!_OS | `apt-get` | `systemd` |
| **RHEL / Fedora-based** | Fedora 38/39/40, RHEL 9, AlmaLinux, Rocky Linux | `dnf` / `yum` | `systemd` |
| **Arch-based** | Arch Linux, Manjaro, EndeavourOS | `pacman` | `systemd` |

Architectures supported: `x86_64` (amd64) and `aarch64` (arm64).

---

## 4. Prerequisites & Networking

### Prerequisites
- **Python**: 3.11 or newer on both host and VM.
- **Tools**: `curl`, `systemd`, `iproute2`.
- **OpenCode**: Host has OpenCode installed (or will install via standard script).

### Networking Setup

#### Recommended: VMware Bridged Networking
- Connects the VM network adapter directly to the physical network.
- The VM receives an IP address on the same subnet as the host (e.g. `192.168.1.150`).
- The VM setup script automatically discovers this IP and generates the host command.
- **Firewall**: Ensure port `4096` (TCP) is accessible:
  ```bash
  # UFW (Ubuntu / Debian / Kali)
  sudo ufw allow 4096/tcp
  
  # Firewalld (Fedora / RHEL)
  sudo firewall-cmd --add-port=4096/tcp --permanent && sudo firewall-cmd --reload
  ```

#### Alternate: VMware NAT with Port Forwarding
If using VMware NAT networking:
1. Open VMware Virtual Network Editor (`vmnetcfg` / Virtual Network Settings).
2. Under VMnet8 (NAT), select **NAT Settings** -> **Add Port Forwarding**:
   - Host Port: `4096` (or `4097` for VM 2)
   - Virtual Machine IP: Guest VM internal IP (e.g. `192.168.122.10`)
   - Virtual Machine Port: `4096`
3. Export the override before running the setup script on the VM:
   ```bash
   export OPENCODE_VM_IP="127.0.0.1"
   bash vm/setup_opencode_vm.sh
   ```

---

## 5. User Workflow

### Inside VMware Linux VM

Run the automated setup script inside the VM:

```bash
bash vm/setup_opencode_vm.sh
```

The script automatically:
1. Detects Linux distribution and hardware architecture.
2. Installs required system packages (`python3`, `curl`, `systemd`).
3. Installs or verifies the OpenCode CLI.
4. Generates unique authentication credentials (`chmod 0600`).
5. Configures and enables the `opencode-vm-agent.service` systemd service.
6. Detects the reachable IPv4 address.
7. Discovers configured models from OpenCode.
8. Displays the single host registration command.

**Example VM Output:**
```text
==========================================================
 OPENCODE VM SETUP COMPLETE!
==========================================================
 VM Identifier: vm-ubuntu-worker (vm-7f3a)
 Reachable IP:  192.168.1.150:4096
 Token TTL:     15 minutes

 Run this command ON YOUR HOST MACHINE to register this VM:

   curl -fsSL "http://192.168.1.150:4096/register/tok_8f92b7c4d1e2..." | bash

==========================================================
```

### On the Host Machine

Copy and run the generated command on the host terminal **exactly once**:

```bash
curl -fsSL "http://192.168.1.150:4096/register/tok_8f92b7c4d1e2..." | bash
```

The host installer automatically:
1. Connects to the VM and verifies the registration token.
2. Invalidates the token on the VM (single-use protection).
3. Exchanges the token for permanent API credentials.
4. Starts the Host Gateway service on `127.0.0.1:8080` if not already running.
5. Saves the VM in the persistent SQLite registry (`registry.db`).
6. Discovers all available models and generates collision-free model IDs.
7. Automatically updates `~/.config/opencode/opencode.json` with the `vm-pool` provider.
8. Displays the newly available models.

**Example Host Output:**
```text
==============================================================
 SUCCESS: VM 'vm-ubuntu-worker' Registered with OpenCode Pool
==============================================================
 VM ID:         vm-7f3a
 Endpoint:      http://192.168.1.150:4096
 OpenCode Conf: ~/.config/opencode/opencode.json
 Gateway URL:   http://127.0.0.1:8080/v1

 Newly Available Models:
   [+] vm-ubuntu-worker__anthropic_claude-3-5-sonnet  (Claude 3.5 Sonnet)
   [+] vm-ubuntu-worker__openai_gpt-4o               (GPT-4o)

 All Pool Models (Total across all VMs):
   * vm-ubuntu-worker__anthropic_claude-3-5-sonnet   [VM: vm-7f3a]
   * vm-ubuntu-worker__openai_gpt-4o                [VM: vm-7f3a]
   * pool__anthropic_claude-3-5-sonnet              [Auto-Pool: 1 VM]
==============================================================
```

### Adding Multiple VMs

To register additional VMs, simply repeat `bash vm/setup_opencode_vm.sh` on each VM and run the generated `curl` command on the host.

Each VM registers independently. The Host Gateway merges all models into a single catalog and provides unified load balancing and failover.

---

## 6. Model Routing & Automated Failover

### Globally Unique Identifiers
To prevent naming collisions when multiple VMs configure the same provider or model:
- **VM-Specific Model**: `{vm_name}__{provider}_{model_name}`  
  *Example*: `vm-worker-1__anthropic_claude-3-5-sonnet` (routes specifically to VM 1).
- **Auto-Failover Pool Model**: `pool__{provider}_{model_name}`  
  *Example*: `pool__anthropic_claude-3-5-sonnet` (routes to any healthy VM providing Claude 3.5 Sonnet).

### Failover Policy & Rate-Limit Handling

1. **HTTP 429 Rate-Limit Detection**:
   - When a VM's upstream provider returns `429 Too Many Requests` or quota exhaustion, the VM Agent surfaces this to the Gateway.
   - The Gateway inspects the `Retry-After` header (or uses configurable default, e.g. 60 seconds) and marks the VM as rate-limited in SQLite.
2. **Transparent Failover**:
   - If the request targets a pooled model or if cross-VM failover is enabled, the Gateway immediately selects the next healthy VM offering the compatible model.
   - The request is retried against the alternate VM.
3. **Idempotency Safeguard**:
   - Retries only occur if zero bytes/tokens have been sent to the client.
   - If a failure happens mid-stream after partial chunks have been delivered, the stream terminates cleanly with an error rather than duplicating partial output.
4. **Loop & Exhaustion Protection**:
   - The Gateway enforces `max_retries` (default 3) to prevent cascading loops.
   - If all candidate VMs are on cooldown, the Gateway returns HTTP 429 with the shortest remaining `Retry-After` time.
5. **Cooldown Recovery**:
   - Once a VM's cooldown window expires, the background health monitor re-checks the VM and marks it active again.

---

## 7. Security Architecture

- **Cryptographically Secure Credentials**:
  - API tokens and registration tokens generated with `secrets.token_urlsafe(32)` (256 bits of entropy).
- **Time-Limited & Single-Use**:
  - Registration tokens expire after 15 minutes.
  - Verification immediately marks the token as used; replay attempts receive HTTP 403.
- **No Secrets in URLs**:
  - Registration tokens are validated over POST; API tokens are never sent in query strings.
  - All communication uses `Authorization: Bearer <token>` headers.
- **Restrictive File Permissions**:
  - Configuration files (`agent_config.json`, `registry.db`) saved with `0600` permissions.
  - Parent directories enforced with `0700`.
- **Input Sanitization & Safe Subprocesses**:
  - No shell expansions or `shell=True` with user inputs.
  - Structured logging filters and masks secret values.
- **Host Revocation**:
  - VMs can be revoked from the host at any time via `scripts/remove_vm.sh <vm_id>`, deleting keys and purging models from OpenCode.

---

## 8. CLI Reference & Management

| Command | Location | Description |
|---|---|---|
| `bash vm/setup_opencode_vm.sh` | VM | Sets up VM agent, starts systemd service, outputs registration command |
| `bash scripts/install_host.sh` | Host | Installs host gateway and user systemd service |
| `bash scripts/register_vm.sh <URL> <TOKEN>` | Host | Manually registers a VM by URL and token |
| `bash scripts/update_vm.sh [VM_ID\|all]` | Host | Refreshes model catalog from one or all registered VMs |
| `bash scripts/remove_vm.sh <VM_ID>` | Host | Unregisters a VM and removes its models from OpenCode |

### Useful Host Gateway Endpoints

- `GET http://127.0.0.1:8080/health`: Gateway status, connected VMs count, healthy VMs count.
- `GET http://127.0.0.1:8080/v1/models`: OpenAI-compatible list of all aggregated models.
- `POST http://127.0.0.1:8080/v1/chat/completions`: OpenAI-compatible completions endpoint.
- `GET http://127.0.0.1:8080/api/vms`: List all registered VMs with health and rate-limit details.

---

## 9. Troubleshooting Guide

### 1. Registration command fails: "Connection refused"
- **Cause**: Firewall blocking port 4096 on the VM or wrong IP detected.
- **Fix**:
  - Run `sudo ufw allow 4096/tcp` on the VM.
  - Check VM IP: `ip -4 addr show`. If running NAT, configure VMware port forwarding.

### 2. "Registration token is invalid or has expired"
- **Cause**: Token TTL expired (default 15 minutes) or token was already used.
- **Fix**:
  - Re-run `bash vm/setup_opencode_vm.sh` on the VM to generate a fresh token.

### 3. Models not appearing in OpenCode
- **Cause**: OpenCode client has cached previous configuration.
- **Fix**:
  - Run `opencode models --refresh` on the host.
  - Inspect `~/.config/opencode/opencode.json` to verify the `"vm-pool"` block exists.

### 4. VM Agent systemd service inspection
- **Check status**: `systemctl status opencode-vm-agent.service`
- **View logs**: `journalctl -u opencode-vm-agent.service -f`

---

## 10. Automated Testing Suite

The project includes unit and integration tests using Python's standard `unittest` framework:

```bash
python3 -m unittest discover -s tests -v
```

### Test Coverage Highlights
- `tests/test_vm_config.py`: Token generation, 15-minute expiration, single-use consumption, file permissions (0600).
- `tests/test_vm_agent.py`: Health checks, Bearer authentication, streaming SSE, non-streaming completions, 429 handling.
- `tests/test_registry.py`: SQLite persistence, idempotent duplicate registrations, model collision prevention, cooldowns.
- `tests/test_model_discovery.py`: Discovered model synchronization, safe updating of `opencode.json` while preserving user settings.
- `tests/test_model_router.py`: Direct routing, automatic 429 rate-limit failover, cooldown tracking, max retries limit.
- `tests/test_health_monitor.py`: Periodic health checks, offline transitions, automatic reconnection recovery.
- `tests/test_integration.py`: End-to-end multi-VM lifecycle test covering provisioning, registration, aggregation, completion, and failover.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

