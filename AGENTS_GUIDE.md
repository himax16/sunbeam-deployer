# Sunbeam Deployer — LLM Agent Operations Guide

> **Purpose**: This document provides complete instructions for an AI/LLM agent
> to autonomously run, monitor, troubleshoot, and manage Sunbeam OpenStack
> deployments using the `sunbeam-deployer` CLI tool.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites & Environment Setup](#2-prerequisites--environment-setup)
3. [Configuration](#3-configuration)
4. [Running a Deployment](#4-running-a-deployment)
5. [Monitoring & Progress Tracking](#5-monitoring--progress-tracking)
6. [Phase Reference](#6-phase-reference)
7. [Troubleshooting & Error Recovery](#7-troubleshooting--error-recovery)
8. [Common Error Patterns & Fixes](#8-common-error-patterns--fixes)
9. [Post-Deployment Verification](#9-post-deployment-verification)
10. [Testflinger Job Management](#10-testflinger-job-management)
11. [Architecture & Internals](#11-architecture--internals)
12. [Timing Reference](#12-timing-reference)
13. [Complete CLI Reference](#13-complete-cli-reference)
14. [Decision Trees](#14-decision-trees)

---

## 1. Overview

### What This Tool Does

`sunbeam-deployer` automates the deployment of [Canonical Sunbeam](https://microstack.run/)
(a Canonical OpenStack distribution) on physical or virtual machines provisioned via
[Testflinger](https://canonical-testflinger.readthedocs-hosted.com/latest/).

The deployment process:
1. **Provisions a machine** via Testflinger (or connects to an existing one)
2. **Creates LXD virtual machines** on that machine using Terraform
3. **Installs the OpenStack snap** in each VM and prepares them for clustering
4. **Bootstraps a Sunbeam cluster** on the first VM and joins the remaining VMs

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Agent Machine (where sunbeam-deployer runs)                │
│                                                              │
│  sunbeam-deployer CLI                                        │
│       │                                                      │
│       ├── testflinger CLI (local) ──► Testflinger Server     │
│       │                                                      │
│       └── SSH ──────────────────────────────────────────┐    │
│                                                         │    │
│  ┌──────────────────────────────────────────────────────▼──┐ │
│  │  Testflinger Machine (remote, bare metal)               │ │
│  │                                                          │ │
│  │  LXD host                                                │ │
│  │  ├── Terraform (creates VMs + networks)                  │ │
│  │  ├── bm0 (LXD VM) ── bootstrap node                     │ │
│  │  ├── bm1 (LXD VM) ── join node                          │ │
│  │  ├── bm2 (LXD VM) ── join node                          │ │
│  │  └── dns (LXD container) ── dnsmasq                     │ │
│  └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

All commands after Phase 0 execute on the remote machine via SSH.
Commands inside LXD VMs execute via `lxc exec` (which itself runs over SSH).

---

## 2. Prerequisites & Environment Setup

### Required Tools on the Agent Machine

| Tool | Purpose | Install |
|------|---------|---------|
| **uv** | Python project manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **testflinger** (CLI) | Submit/poll Testflinger jobs | `sudo snap install testflinger-cli` |
| **ssh** | Connect to remote machines | Pre-installed on Ubuntu |
| **git** | Clone repositories | Pre-installed on Ubuntu |

### Required Access

- **Network**: Access to the Testflinger server (`https://testflinger.canonical.com`) and
  the snap store
- **SSH keys**: Either an SSH key pair or Launchpad/GitHub keys configured for Testflinger
  machine access
- **Testflinger queue access**: The target queue (e.g. `openstack`) must be accessible

### Initial Setup

```bash
# 1. Navigate to the project directory
cd /path/to/sunbeam-deploy-bot

# 2. Install Python dependencies
uv sync

# 3. Verify the tool works
uv run sunbeam-deployer --version
# Expected output: sunbeam-deployer 0.1.0

# 4. Verify testflinger CLI is available
testflinger --help
```

### File System Layout

```
sunbeam-deploy-bot/          # Project root
├── sunbeam_deployer/        # Python package
│   ├── __main__.py          # CLI entry point (argparse, subcommand dispatch)
│   ├── commands.py          # Subcommand handlers (list-jobs)
│   ├── config.py            # Config loading
│   ├── executor.py          # Command execution engine
│   ├── logger.py            # Dual logging system
│   ├── monitor.py           # Progress tracking
│   ├── phases/              # Deployment phases
│   │   ├── testflinger.py   # Phase 0
│   │   ├── host_setup.py    # Phase 1
│   │   ├── vm_deploy.py     # Phase 2
│   │   └── cluster.py       # Phase 3
│   └── scripts/
│       └── host-setup.sh    # Standalone bash script for Phase 1
├── config.example.yaml      # Reference configuration
├── pyproject.toml            # Project metadata
└── logs/                    # Log files (created at runtime)
```

---

## 3. Configuration

### Configuration File

The tool reads a YAML configuration file. If no config file is provided, built-in
defaults are used. To create a config file:

```bash
cp config.example.yaml config.yaml
```

### Key Configuration Sections

#### 3.1 Testflinger Settings

```yaml
testflinger:
  enabled: true                    # Enable Testflinger provisioning
  # job_id: <UUID>                 # Attach to existing job (skip submit)
  # job_file: ~/testflinger-job.yaml  # Submit specific YAML
  queue: openstack                 # Testflinger queue name
  distro: noble                    # Ubuntu version to provision
  reserve_timeout: 259200          # 3 days reservation
  ssh_keys:                        # Keys for reserve access
    - lp:himax16
    - gh:himax16
  ssh_user: ubuntu                 # SSH user on provisioned machine
  # ssh_key_path: ~/.ssh/id_rsa   # Private key path (optional)
  provision_timeout: 1800          # Max wait for provisioning
```

#### 3.2 Snap Settings

```yaml
snap:
  source: store                    # "store" or "local"
  channel: 2024.1/edge             # Snap store channel
  # revision: null                 # Pin specific revision
  # local_path: ~/openstack.snap  # Path for local installs
  # install_method: dangerous      # "dangerous" or "try"
```

#### 3.3 Sunbeam Cluster Settings

```yaml
sunbeam:
  roles: [control, compute, storage]  # Default roles for all nodes
  node_roles:                          # Per-node role overrides
    bm0: [control, compute, storage]
    bm1: [compute, storage]
    bm2: [compute]
  manifest: true                   # Push Terraform manifest to VMs
  accept_defaults: false           # Use --accept-defaults on bootstrap
  cluster_node_count: 0            # 0 = all nodes; N = first N nodes
```

**`cluster_node_count`**: Controls how many VMs join the Sunbeam cluster.

- `0` (default) — all Terraform VMs join the cluster (backward-compatible).
- `1` — single-node cluster: bootstrap only, no join, DNS validation skipped.
- `N` — first *N* VMs (in Terraform output order) join; remaining VMs are
  still fully deployed in Phase 2 (snap installed, prepared) but excluded
  from Phase 3 (no bootstrap/join).

**Role Priority**: `config node_roles` > `global roles default` > `Terraform output roles`

#### 3.4 Timeouts

```yaml
timeouts:
  cloud_init_wait: 600     # VM cloud-init (10 min)
  snap_install: 600        # Snap installation (10 min)
  prepare_node: 600        # prepare-node-script (10 min)
  cluster_bootstrap: 7200  # Bootstrap (2 hours — typically 30-60 min)
  cluster_join: 3600       # Join per node (1 hour — typically 40-45 min)
  terraform_apply: 3600    # Terraform apply (1 hour)
```

#### 3.5 Concurrency

```yaml
concurrency:
  vm_deploy: 2    # Max VMs deployed in parallel
```

#### 3.6 Terraform Settings

```yaml
terraform:
  extra_args: []            # Extra args passed to bootstrap.sh
  bootstrap_retries: 1      # Auto-retry on VM boot timeout (0 = no retry)
```

The LXD provider in the Terraform module has a hardcoded 3-minute VM boot
timeout. On slower hardware, VMs may take longer to start. `bootstrap_retries`
automatically retries failed `terraform apply` calls — Terraform is idempotent,
so the second attempt picks up where the first left off.

### CLI Overrides

All key settings can be overridden via CLI flags (see [CLI Reference](#13-complete-cli-reference)).
CLI flags always take priority over config file values.

---

## 4. Running a Deployment

The CLI has two subcommands: `deploy` (the default) and `list-jobs`. When no
subcommand is given, `deploy` is implied for backward compatibility.

### Deployment Modes

#### Mode 1: Full Deployment with New Testflinger Job

```bash
uv run sunbeam-deployer --testflinger -c config.yaml
# equivalent:
uv run sunbeam-deployer deploy --testflinger -c config.yaml
```

This submits a new Testflinger job, waits for provisioning, then runs all phases.

#### Mode 2: Attach to Existing Testflinger Job

```bash
uv run sunbeam-deployer --tf-job-id <JOB_UUID>
```

Attaches to a job that's already in `reserve` state. Skips job submission.

#### Mode 3: Direct SSH to a Machine

```bash
uv run sunbeam-deployer --device-ip 10.241.2.45
```

Skips Testflinger entirely. Connects directly via SSH to the given IP.

#### Mode 4: Local Machine (No SSH)

```bash
uv run sunbeam-deployer
```

Runs all host commands locally. The machine running the tool IS the deployment target.

### list-jobs — List Testflinger Jobs

```bash
# Show only active/reserve jobs (default)
uv run sunbeam-deployer list-jobs

# Show all jobs including waiting/completed
uv run sunbeam-deployer list-jobs --all

# Machine-readable JSON output
uv run sunbeam-deployer list-jobs --all --format json
```

### Running Individual Phases

When a deployment partially succeeds, you can re-run specific phases:

```bash
# Re-run only host setup (LXD + Terraform)
uv run sunbeam-deployer --device-ip 10.241.2.45 --phase host-setup

# Re-run only VM deployment (snap install + prepare)
uv run sunbeam-deployer --device-ip 10.241.2.45 --phase vm-deploy

# Re-run only cluster bootstrap + join
uv run sunbeam-deployer --device-ip 10.241.2.45 --phase cluster
```

**Important**: When running `--phase vm-deploy` or `--phase cluster`, the tool
automatically reconstructs VM metadata from existing Terraform outputs. The
infrastructure must already be set up.

### Verbose Output

```bash
uv run sunbeam-deployer -v --device-ip 10.241.2.45
```

The `-v` flag enables DEBUG-level terminal output, showing every command executed
and its full output. Without `-v`, only INFO-level messages appear on the terminal
(full DEBUG output always goes to the log file).

---

## 5. Monitoring & Progress Tracking

### Real-Time Terminal Output

The deployer shows colored, phase-tagged output:

```
14:32:01 ━━━ Phase: host-setup ━━━
14:32:01   ▸ Install and initialise LXD
14:32:05   ▸ Install Terraform snap
14:32:10   ▸ Clone infrastructure repository
14:32:15   ▸ Run infrastructure bootstrap (Terraform)
14:37:22   ▸ Parse Terraform outputs
14:37:22 ✅ Phase 'host-setup' success (5m21s)
```

Each line includes:
- **Timestamp** (HH:MM:SS)
- **Phase/step prefix** in brackets
- **Status symbols**: ✅ success, ❌ failed, ⏳ pending, 🔄 running, ⏭️ skipped

### Log Files

Full debug logs are written to:

```
./logs/sunbeam-deploy-YYYYMMDD-HHMMSS.log
```

The log file includes:
- Every command executed (with full arguments)
- Every line of command output (prefixed with `  | `)
- Timing information for all operations
- SSH connection details (secrets redacted)

**To read the log file**:
```bash
# Follow in real-time
tail -f logs/sunbeam-deploy-*.log

# Search for errors
grep -i "error\|failed\|exception" logs/sunbeam-deploy-*.log

# Find the log for the latest run
ls -lt logs/ | head -5
```

### Deployment Summary

At the end of each run (success or failure), a summary table is printed:

```
╔══════════════════════════════════════════════════════╗
║            Sunbeam Deployment Summary                ║
╚══════════════════════════════════════════════════════╝
  Total time: 1h38m22s

  ✅ testflinger (0m05s)
      ✅ Wait for provisioning (0m02s)
      ✅ Establish SSH to 10.241.2.45 (0m03s)
  ✅ host-setup (5m21s)
      ✅ Install and initialise LXD (0m04s)
      ✅ Install Terraform snap (0m05s)
      ✅ Clone infrastructure repository (0m03s)
      ✅ Run infrastructure bootstrap (Terraform) (4m55s)
      ✅ Parse Terraform outputs (0m14s)
  ✅ vm-deploy (4m12s)
      ✅ Wait for bm0 to be ready (0m35s)
      ✅ Install openstack snap on bm0 (1m22s)
      ...
  ✅ cluster (1h28m44s)
      ✅ Validate cross-node DNS resolution (0m06s)
      ✅ Bootstrap Sunbeam cluster on bm0 (39m12s)
      ✅ Join bm1 to cluster (42m33s)
      ✅ Join bm2 to cluster (40m55s)

  Overall: ✅ SUCCESS
```

### Monitoring from Another Terminal

While a deployment is running, an agent can monitor progress from a separate session:

```bash
# 1. Find the latest log file
LOG=$(ls -t logs/sunbeam-deploy-*.log | head -1)

# 2. Follow the log in real-time
tail -f "$LOG"

# 3. Check which phase is currently running
grep "━━━ Phase:" "$LOG" | tail -1

# 4. Check for errors so far
grep -c "FAILED\|ERROR" "$LOG"

# 5. Check the last few lines of output
tail -20 "$LOG"
```

### Programmatic Status Checks

If the deployment is running against a Testflinger job, you can check the job status:

```bash
# Check job phase
testflinger status <JOB_UUID>

# Get full results (includes device_ip, agent_name)
testflinger results <JOB_UUID>

# Cancel the job (releases the machine)
testflinger cancel <JOB_UUID>
```

---

## 6. Phase Reference

### Phase 0: Testflinger (`testflinger`)

**Purpose**: Provision a machine or connect to an existing one.

**Steps**:
1. **submit-job**: Submit a Testflinger job YAML (or attach to existing)
2. **wait-provision**: Poll until the job reaches `reserve` state
3. **ssh-connect**: Verify SSH connectivity and configure the executor

**Inputs**: Testflinger config (queue, distro, SSH keys)
**Outputs**: SSH connection to the provisioned machine

**Testflinger Job Phases** (in order):
```
setup → provision → firmware_update → test → allocate → reserve → cleanup
```
The tool waits for `reserve` (or `test`) before proceeding.

**Key Details**:
- `testflinger submit --quiet <file>` returns just the job UUID
- `testflinger status <id>` returns the current phase name
- `testflinger results <id>` returns JSON with `device_info.device_ip`
- Polling starts at 15s intervals, slows to 30s after 5 minutes

### Phase 1: Host Setup (`host-setup`)

**Purpose**: Install infrastructure tooling and create LXD VMs.

Phase 1 delegates to a **standalone bash script** (`scripts/host-setup.sh`) that can
also be run independently on any Ubuntu machine. The Python tool pipes the script over
SSH with environment variables from configuration.

**Steps** (inside `host-setup.sh`):
1. **Install LXD** snap (idempotent)
2. **Initialize LXD** with `lxd init --auto` (idempotent)
3. **Install Terraform** snap — classic confinement (idempotent)
4. **Clone/pull repo** — `himax16/sunbeam-proxified-dev` (idempotent)
5. **Run bootstrap.sh** — Terraform init/apply, creates VMs + networks

After the script completes, Python re-runs `_parse_terraform_outputs()` to read
`terraform output -json compute_nodes` and `network_topology`.

**Outputs**: `InfraInfo` containing:
- List of `ComputeNode` objects (name, fqdn, hostname, ip, roles, osd_devices)
- Paths to manifest.yaml, ssh_private_key, plan directory
- Management domain name

**Key Details**:
- VMs are named `bm0`, `bm1`, `bm2`, ... with FQDNs `bm0.res`, `bm1.res`, etc.
- Management network: `192.167.98.0/24`, VMs start at `.10`
- A DNS container (dnsmasq) provides name resolution within the LXD network
- Pre-initializing LXD with `--auto` is critical to avoid `bootstrap.sh` bugs

### Phase 2: VM Deployment (`vm-deploy`)

**Purpose**: Install the OpenStack snap and prepare each VM for Sunbeam.

**Per-VM Steps** (runs in parallel, max `concurrency.vm_deploy` at a time):
1. **wait-ready**: Wait for LXD agent + cloud-init to complete
2. **install-snap**: Install `openstack` snap (from store or local file)
3. **alias**: Set up `sunbeam` snap alias
4. **interfaces** (local install only): Connect snap interfaces manually
5. **prepare**: Run `sunbeam prepare-node-script --bootstrap | bash -x`
6. **manifest**: Push `manifest.yaml` from Terraform output into the VM

**Key Details**:
- Snap install is idempotent — checks `snap list openstack` first
- `newgrp snap_daemon` is NOT called in automation; fresh `lxc exec` sessions
  inherit the group from the login shell
- Manifest is pushed via `lxc file push` on the host (not local filesystem)

### Phase 3: Cluster (`cluster`)

**Purpose**: Bootstrap the Sunbeam cluster and join additional nodes. Only nodes
selected by `cluster_node_count` participate (default: all).

**Steps**:
1. **Resolve cluster nodes**: Select the first *N* VMs (or all when count is 0)
2. **dns-validation**: Verify every cluster node can `getent hosts` every other
   cluster node's FQDN (skipped for single-node clusters)
3. **bootstrap-bm0**: Run `sunbeam -v cluster bootstrap` on the first selected node
4. **join-bm1**: Generate token on bm0, join bm1
5. **join-bm2**: Generate token on bm0, join bm2
6. ... (one join step per additional cluster node)

**Key Details**:
- **Token generation**: `sunbeam cluster add --format yaml <FQDN>` — `<FQDN>` is positional
- **Token extraction**: Parses YAML `token:` field or falls back to bare base64 regex
- **Join command**: `sunbeam -v cluster join --role <roles> <TOKEN>` — `<TOKEN>` is positional
- Join is sequential (one node at a time) to avoid cluster races
- Bootstrap typically takes 30-60 minutes; joins take 40-45 minutes each
- **Single-node cluster**: When `cluster_node_count: 1`, DNS validation and join
  steps are skipped entirely; only bootstrap runs
- Non-cluster VMs (excluded by count) still have the snap installed and are prepared
  in Phase 2, enabling later manual expansion

---

## 7. Troubleshooting & Error Recovery

### General Approach

1. **Read the log file** — it contains every command and output:
   ```bash
   LOG=$(ls -t logs/sunbeam-deploy-*.log | head -1)
   grep "FAILED\|ERROR\|error\|failed" "$LOG" | tail -20
   ```

2. **Check the summary** — identify which phase/step failed

3. **Re-run the failed phase** using `--phase <name>`:
   ```bash
   uv run sunbeam-deployer --device-ip <IP> --phase <failed-phase>
   ```

4. **Check Sunbeam logs inside the VM** (for cluster phase failures):
   ```bash
   # SSH to the Testflinger machine
   ssh ubuntu@<device-ip>

   # Check sunbeam logs inside the VM
   lxc exec bm0 -- cat /home/ubuntu/snap/openstack/common/logs/sunbeam.log
   ```

### Phase-Specific Recovery

#### Host Setup Failures

| Symptom | Action |
|---------|--------|
| LXD install fails | SSH in, check `snap list lxd`, try `sudo snap install lxd` manually |
| Terraform apply fails | SSH in, `cd ~/sunbeam-proxified-dev/manual-infra && terraform plan` to diagnose |
| bootstrap.sh fails | Check if LXD is initialized: `lxc storage show default` |
| Repo clone fails | Check network, try `git clone <url>` manually |

#### VM Deploy Failures

| Symptom | Action |
|---------|--------|
| VM not responsive | `lxc list` to check VM state, `lxc start bm0` if stopped |
| cloud-init timeout | `lxc exec bm0 -- cloud-init status` to check |
| snap install fails | `lxc exec bm0 -- sudo snap install openstack --channel=2024.1/edge` manually |
| prepare-node fails | Check snap connections: `lxc exec bm0 -- sudo snap connections openstack` |

#### Cluster Failures

| Symptom | Action |
|---------|--------|
| DNS validation fails | Check DNS container: `lxc exec dns -- cat /etc/dnsmasq.d/*.conf` |
| Bootstrap timeout | Check sunbeam logs: `lxc exec bm0 -- cat /home/ubuntu/snap/openstack/common/logs/sunbeam.log` |
| Token generation fails | Verify bootstrap succeeded: `lxc exec bm0 -- sudo -iu ubuntu sunbeam cluster list` |
| Join fails | Check the token is valid, verify DNS from the joining node |

### Manual Intervention via SSH

```bash
# Connect to the Testflinger machine
ssh ubuntu@<device-ip>

# List all VMs
lxc list

# Enter a VM as ubuntu
lxc exec bm0 -- sudo -iu ubuntu bash

# Check cluster status (inside bm0)
sunbeam cluster list

# Check snap status (inside any VM)
snap list openstack
snap services openstack

# Check Sunbeam logs (inside any VM)
tail -100 ~/snap/openstack/common/logs/sunbeam.log
```

---

## 8. Common Error Patterns & Fixes

These are real bugs discovered during live testing. An agent should recognize
these patterns and apply the documented fixes.

### 8.1 `~` Expansion in Remote Commands

**Pattern**: `os.path.expanduser("~")` returns the LOCAL user's home directory
(e.g. `/home/agent@company.com/`) instead of the remote user's home (`/home/ubuntu/`).

**Fix**: Never expand `~` locally for remote paths. Keep `~` in strings and let
the remote bash shell expand it. Use `run_host("test -d ~/somedir")` instead of
`Path("~/somedir").exists()`.

### 8.1b `shlex.quote` Blocks `~` Expansion

**Pattern**: `shlex.quote("~/dir")` produces `'~/dir'` — single quotes prevent `~`
from being expanded by bash. The env var reaches the script as the literal string
`~/dir` including the single quotes.

**Fix**: For values that start with `~`, do NOT wrap with `shlex.quote()`. Pass
the value bare in the env var assignment so the remote bash expands it:
`f"REPO_DIR={cfg.repo_dir}"` instead of `f"REPO_DIR={shlex.quote(cfg.repo_dir)}"`.

### 8.2 Terraform `-chdir=~/path` Fails

**Pattern**: `terraform -chdir=~/sunbeam-proxified-dev/manual-infra output`
fails because bash doesn't expand `~` inside flag values (only at word start).

**Fix**: Use `cd ~/path && terraform output` instead of `-chdir=~/path`.

### 8.3 LXD Init Preseed Failure

**Pattern**: `bootstrap.sh` uses a dir-driver preseed but the machine has block
devices. The LXD init step in `bootstrap.sh` fails.

**Fix**: Pre-initialize LXD with `lxd init --auto` before running `bootstrap.sh`.
The script detects an existing LXD installation and skips its own init.

### 8.4 Local Filesystem Checks for Remote Files

**Pattern**: `os.path.exists("/some/path")` or `Path("/some/path").exists()` checks
the local filesystem instead of the remote machine's filesystem.

**Fix**: Use `run_host("test -f /some/path")` for remote file existence checks.

### 8.5 Snap Install Not Idempotent

**Pattern**: `sudo snap install openstack` fails if already installed with
"snap \"openstack\" is already installed".

**Fix**: Check `snap list openstack` first, skip install if already present.

### 8.6 `sunbeam cluster add` Syntax

**Pattern**: Using `sunbeam cluster add --name bm1.res` fails because `--name`
is not a valid flag.

**Fix**: The node name is a positional argument:
`sunbeam cluster add --format yaml bm1.res`

### 8.7 `sunbeam cluster join` Syntax

**Pattern**: Using `sunbeam cluster join --token <TOKEN>` fails because `--token`
is not a valid flag.

**Fix**: The token is a positional argument:
`sunbeam cluster join --role control,compute,storage <TOKEN>`

### 8.8 Monitor Phase Name Mismatch

**Pattern**: When running `--phase cluster`, the tool tries to reconstruct
infrastructure from Terraform outputs but uses the wrong phase constant for
the monitor.

**Fix**: Import `PHASE as HS_PHASE` from `host_setup` and use it when creating
the temporary monitor for reconstruction.

---

## 9. Post-Deployment Verification

After a successful deployment, verify the cluster is healthy:

```bash
# SSH to the Testflinger machine
ssh ubuntu@<device-ip>

# Enter the primary VM
lxc exec bm0 -- sudo -iu ubuntu bash

# Check cluster status
sunbeam cluster list

# Expected output (3-node cluster):
# Name    Status   Control  Compute  Storage
# bm0.res running  active   active   active
# bm1.res running  active   active   active
# bm2.res running  active   active   active

# Check Juju status
juju status

# Check OpenStack services
sunbeam openstack status
```

### Health Checks

| Check | Command (inside bm0) | Expected |
|-------|---------------------|----------|
| Cluster members | `sunbeam cluster list` | All nodes "running", roles "active" |
| Juju models | `juju models` | Models created and active |
| Services | `juju status` | All units active/idle |

---

## 10. Testflinger Job Management

### Job Lifecycle

```
Submit → Queued → Setup → Provision → Reserve → (Work) → Cancel/Cleanup
```

### Common Operations

```bash
# Submit a new job
testflinger submit --quiet <job-file.yaml>
# Returns: <job-uuid>

# Check current phase
testflinger status <job-uuid>
# Returns: reserve

# Get device IP and agent info
testflinger results <job-uuid>
# Returns JSON with device_info.device_ip and device_info.agent_name

# Cancel a job (release the machine)
testflinger cancel <job-uuid>

# Poll a job (stream output to terminal)
testflinger poll <job-uuid>
```

### Testflinger Job YAML Structure

When the tool auto-generates a job YAML, it creates:

```yaml
job_queue: openstack
global_timeout: 262800       # reserve_timeout + 3600
output_timeout: 900          # 15 minutes
provision_data:
  distro: noble
reserve_data:
  timeout: 259200             # 3 days
  ssh_keys:
    - lp:himax16
    - gh:himax16
```

### Important Notes

- **Jobs time out**: The `reserve_timeout` controls how long you have the machine.
  Default is 3 days (259200 seconds).
- **Job IDs are UUIDs**: e.g. `97c62a00-2522-414b-b296-1fe77a6bbd99`

### Post-Deployment Job Handling

- **Submitted jobs** (via `--testflinger` or `--tf-job-file`): After a successful
  deployment, the tool prompts interactively:
  ```
  Cancel Testflinger job <UUID> and release the machine? [y/N]
  ```
  Default is **No** — the machine stays alive for inspection or further work.
- **Attached jobs** (via `--tf-job-id`): No prompt. The tool logs the job ID and
  how to cancel manually (`testflinger cancel <UUID>`).
- **`--cancel-on-failure`**: Automatically cancels the job on deployment failure
  without prompting. Only applies to failure scenarios.
- **Manual cancel**: `testflinger cancel <UUID>` releases the machine at any time.

---

## 11. Architecture & Internals

### Execution Model

The tool uses a transparent SSH routing system:

1. **`run_local(cmd)`**: Always runs on the agent machine. Used for `testflinger` CLI.
2. **`run_host(cmd)`**: Runs on the deployment host. If a remote target is set (via
   Testflinger or `--device-ip`), commands are wrapped in SSH. Otherwise runs locally.
3. **`run_in_vm(vm, cmd)`**: Runs inside an LXD VM via `lxc exec <vm> -- sudo -iu ubuntu bash -lc '<cmd>'`.
   The `lxc exec` command itself goes through `run_host()`, so it's also SSH-wrapped
   when a remote target is set.

This means all phase code works identically whether running locally or remotely —
the routing is handled at the executor level.

### Data Flow Between Phases

```
Phase 0 (Testflinger) ──► sets RemoteTarget (SSH host/user/key)
                           │
Phase 1 (Host Setup) ─────► returns InfraInfo:
                           │   - ComputeNode[] (name, fqdn, ip, roles)
                           │   - manifest_path, ssh_key_path, plan_dir
                           │
Phase 2 (VM Deploy) ──────► uses InfraInfo to deploy each node
                           │
Phase 3 (Cluster) ────────► uses InfraInfo for bootstrap + join
```

When running a single phase (e.g. `--phase cluster`), the tool reconstructs
`InfraInfo` from existing Terraform outputs by re-running `_parse_terraform_outputs()`.

### Terraform Outputs

The tool reads these Terraform outputs as its source of truth:

**`compute_nodes`** (JSON array):
```json
[
  {
    "name": "bm0",
    "fqdn": "bm0.res",
    "hostname": "bm0",
    "ip": "192.167.98.10",
    "roles": ["control", "compute", "storage"],
    "osd_devices": ["/dev/sdb"]
  },
  ...
]
```

**`network_topology`** (JSON object):
```json
{
  "management": {
    "domain": "res",
    "network": "192.167.98.0/24",
    ...
  }
}
```

### Secret Redaction

The logger automatically redacts sensitive patterns from both file and terminal logs:
- `token: <value>` → `token: <REDACTED>`
- SSH private keys → `<REDACTED>`
- `apikey: <value>` → `apikey: <REDACTED>`
- `password: <value>` → `password: <REDACTED>`

### Concurrency Model

- **Phase 2 (VM Deploy)** uses a `ThreadPoolExecutor` with configurable max workers
  (default: 2). Each VM is deployed in its own thread.
- **Phase 3 (Cluster)** is strictly sequential: bootstrap first, then join one
  node at a time to avoid cluster coordination races. Only nodes selected by
  `cluster_node_count` participate.
- **DNS validation** checks all N×(N-1) pairs among selected cluster nodes before
  any clustering begins. Skipped for single-node clusters.

---

## 12. Timing Reference

Based on real deployment data (3-node cluster on Testflinger):

| Operation | Typical Duration | Timeout Default |
|-----------|-----------------|-----------------|
| Testflinger provisioning | 5-30 min | 1800s (30 min) |
| SSH connectivity | 2-10s | 120s |
| Host setup (LXD + Terraform) | 5-6 min | 3600s |
| VM cloud-init | 30-60s | 600s |
| Snap install (per VM) | 1-2 min | 600s |
| prepare-node-script (per VM) | 1-3 min | 600s |
| Cluster bootstrap (bm0) | 30-60 min | 7200s (2 hr) |
| Cluster join (per node) | 40-45 min | 3600s (1 hr) |
| **Total (3-node cluster)** | **1.5-2 hours** | — |
| **Total (single-node cluster)** | **~1 hour** | — |

**Note**: The cluster phase dominates total deployment time. Bootstrap and joins
involve Juju model deployment, charm installation, and service orchestration.
Single-node clusters (`cluster_node_count: 1`) skip join steps entirely.

---

## 13. Complete CLI Reference

```
sunbeam-deployer [-V] [-c FILE] [-v] [--phase PHASE] [flags]
sunbeam-deployer {deploy,list-jobs} ...

Subcommands:
  deploy              Deploy Sunbeam (default if no subcommand given)
  list-jobs           List Testflinger jobs and their IP addresses

General (deploy):
  -V, --version              Show version and exit
  -c, --config FILE          YAML configuration file
  -v, --verbose              DEBUG-level terminal output
  --phase PHASE              Run specific phase:
                               all (default), testflinger, host-setup,
                               vm-deploy, cluster

Testflinger:
  --testflinger              Enable Testflinger provisioning
  --tf-job-id JOB_ID         Attach to existing job UUID
  --tf-job-file FILE         Submit this job YAML
  --tf-ssh-key PATH          SSH private key for the machine
  --device-ip IP             Skip Testflinger, SSH directly to this IP
  --cancel-on-failure        Auto-cancel Testflinger job if deployment fails

Snap:
  --snap-channel CHANNEL     Override snap channel (sets source=store)
  --snap-revision REV        Pin snap revision (sets source=store)
  --snap-file PATH           Install from local .snap file (sets source=local)

Deployment:
  --deploy-mode MODE         Override deploy mode: manual or maas
  --repo-dir DIR             Override repo clone directory
  --accept-defaults          Pass --accept-defaults to sunbeam bootstrap
  --no-manifest              Skip pushing manifest.yaml to VMs
  --tf-arg ARG               Extra terraform arg (repeatable)

list-jobs:
  -v, --verbose              Enable verbose output
  -a, --all                  Show all jobs (default: active/reserve only)
  -f, --format FORMAT        Output format: table (default) or json
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Deployment completed successfully |
| 1 | Deployment failed (config error, phase failure, or unhandled exception) |

---

## 14. Decision Trees

### Which Deployment Mode to Use?

```
Do you have a Testflinger job UUID?
├── Yes → uv run sunbeam-deployer --tf-job-id <UUID>
└── No
    ├── Do you want to provision a new machine?
    │   ├── Yes → uv run sunbeam-deployer --testflinger
    │   └── No
    │       ├── Do you have a machine IP?
    │       │   ├── Yes → uv run sunbeam-deployer --device-ip <IP>
    │       │   └── No → uv run sunbeam-deployer  (runs locally)
    │       └──
    └──
```

### How to Resume After Failure?

```
Which phase failed?
├── testflinger → Fix network/queue issues, re-run with --testflinger
├── host-setup
│   ├── LXD issue → SSH in, fix manually, re-run --phase host-setup
│   ├── Terraform issue → SSH in, cd to plan dir, debug terraform
│   └── bootstrap.sh → Check LXD init, re-run --phase host-setup
├── vm-deploy
│   ├── Single VM → Fix the VM, re-run --phase vm-deploy (idempotent)
│   └── All VMs → Check network, snap store access
└── cluster
    ├── DNS validation → Fix DNS container, re-run --phase cluster
    ├── Bootstrap → Check sunbeam logs in bm0, re-run --phase cluster
    └── Join → Check token, DNS, re-run --phase cluster
```

### Choosing Snap Source

```
Do you have a local .snap file to test?
├── Yes → --snap-file ~/path/to/openstack.snap
│         (also set --snap-method dangerous or try in config)
└── No → Use store (default)
    ├── Need specific channel? → --snap-channel 2024.2/beta
    └── Need specific revision? → --snap-revision 123
```

---

## Appendix: Tools Required by an LLM Agent

To operate this deployment tool, an LLM agent needs the following capabilities:

### Required Tools

| Tool | Purpose | Usage |
|------|---------|-------|
| **Shell/Bash execution** | Run CLI commands | `uv run sunbeam-deployer ...`, `ssh`, `testflinger` |
| **File read** | Read config files and logs | Read `config.yaml`, `logs/*.log` |
| **File write/edit** | Create/modify config files | Write `config.yaml` |
| **SSH** | Connect to remote machines for debugging | `ssh ubuntu@<ip>` |

### Minimum Agent Capabilities

1. **Execute shell commands** and read their output (stdout + stderr + exit code)
2. **Read files** (config files, log files) for monitoring and debugging
3. **Write files** (create config.yaml from template)
4. **Handle long-running processes** — deployments take 1.5-2 hours
5. **Parse structured output** (JSON from testflinger results, deployment summary)
6. **Pattern matching** on log output to identify errors

### Suggested Agent Workflow

```
1. Verify prerequisites (uv, testflinger CLI, SSH access)
2. Create/validate config.yaml
3. Start deployment (uv run sunbeam-deployer ...)
4. Monitor progress:
   a. Tail the log file periodically
   b. Check for error patterns
   c. If stuck, check testflinger job status
5. On completion:
   a. Parse the summary output for success/failure
   b. If success: run verification checks
   c. If failure: identify failed phase, attempt recovery
6. Handle post-deployment cancel prompt:
   - If the job was submitted by the tool, it will prompt to cancel.
   - Answer "y" to release the machine, or "N" to keep it alive.
   - For attached jobs (--tf-job-id), cancel manually when done.
7. Report results with:
   - Overall status (success/failure)
   - Duration per phase
   - Any errors encountered
   - Testflinger job ID (for future reference)
   - Device IP (for SSH access)
```
