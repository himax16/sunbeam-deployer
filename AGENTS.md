# AGENTS.md

## Overview

Automated deployment of [Canonical Sunbeam](https://microstack.run/) (OpenStack) on [Testflinger](https://canonical-testflinger.readthedocs-hosted.com/latest/) bare-metal machines. Python CLI built with [rich-click](https://github.com/ewels/rich-click), managed by [uv](https://docs.astral.sh/uv/) + [hatchling](https://hatch.pypa.io/). Runtime dependencies: `rich-click`, `pyyaml`.

The deployment process:

1. **Provisions a machine** via Testflinger (or connects to an existing one)
2. **Creates LXD virtual machines** on that machine using Terraform
3. **Installs the OpenStack snap** in each VM and prepares them for clustering
4. **Bootstraps a Sunbeam cluster** on the first VM and joins the remaining VMs

### Architecture Diagram

```text
┌──────────────────────────────────────────────────────────────┐
│  Agent Machine (where sunbeam-deployer runs)                 │
│                                                              │
│  sunbeam-deployer CLI                                        │
│       │                                                      │
│       ├── testflinger CLI (local) ──► Testflinger Server     │
│       │                                                      │
│       └── SSH ──────────────────────────────────────────┐    │
│                                                         │    │
│  ┌──────────────────────────────────────────────────────▼──┐ │
│  │  Testflinger Machine (remote, bare metal)               │ │
│  │                                                         │ │
│  │  LXD host                                               │ │
│  │  ├── Terraform (creates VMs + networks)                 │ │
│  │  ├── bm0 (LXD VM) ── bootstrap node                     │ │
│  │  ├── bm1 (LXD VM) ── join node                          │ │
│  │  ├── bm2 (LXD VM) ── join node                          │ │
│  │  └── dns (LXD container) ── dnsmasq                     │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

All commands after Phase 0 execute on the remote machine via SSH. Commands inside LXD VMs execute via `lxc exec` (which itself runs over SSH).

---

## Dev Environment

```bash
uv sync                              # Install dependencies
uv run sunbeam-deployer --help        # Validate CLI loads
uv run sunbeam-deployer --version     # Expect: sunbeam-deployer 0.1.0
```

Add new dependencies to `pyproject.toml` under `[project] dependencies`, then `uv sync`.

**Build note**: Project name is `sunbeam-deployer`, package dir is `sunbeam_deployer`. The mapping lives in `[tool.hatch.build.targets.wheel] packages = ["sunbeam_deployer"]`. Do not rename either without updating this.

### Prerequisites

| Tool | Purpose | Install |
| ------ | --------- | --------- |
| **uv** | Python project manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **testflinger** (CLI) | Submit/poll Testflinger jobs | `sudo snap install testflinger-cli` |
| **ssh** | Connect to remote machines | Pre-installed on Ubuntu |
| **git** | Clone repositories | Pre-installed on Ubuntu |

### Required Access

- **Network**: Access to the Testflinger server (`https://testflinger.canonical.com`) and the snap store
- **SSH keys**: Either an SSH key pair or Launchpad/GitHub keys configured for Testflinger machine access
- **Testflinger queue access**: The target queue (e.g. `openstack`) must be accessible

### File System Layout

```text
sunbeam-deployer/            # Project root
├── sunbeam_deployer/        # Python package
│   ├── __main__.py          # Thin entry point, delegates to cli.py
│   ├── cli.py               # rich-click CLI: group, commands, deploy logic
│   ├── commands.py          # Subcommand handlers (list-jobs)
│   ├── config.py            # Config loading
│   ├── executor.py          # Command execution engine
│   ├── logger.py            # LiveDisplay spinner + rich handler, secret redaction
│   ├── monitor.py           # Phase/step tracking, rich.Table summary
│   ├── phases/              # Deployment phases
│   │   ├── testflinger.py   # Phase 0
│   │   ├── host_setup.py    # Phase 1
│   │   ├── vm_deploy.py     # Phase 2
│   │   └── cluster.py       # Phase 3
│   └── scripts/
│       └── host-setup.sh    # Standalone bash script for Phase 1
├── config.example.yaml      # Reference configuration
└── pyproject.toml           # Project metadata
```

## Testing & Verification

Testing is managed via [tox](https://tox.wiki/) with config in `tox.ini`. Two environments: `unit` (pytest) and `lint` (ruff check + format).

**Always run after any change:**

```bash
uv run python -c "from sunbeam_deployer import __version__; print(__version__)"   # Import check
uv run sunbeam-deployer --help # CLI loads (rich-click rendering)
uv run python -c "from sunbeam_deployer.config import load_config; load_config()"  # Config parses
uv run tox                                                                         # All checks (unit + lint)
uv run tox -e unit                                                                 # Unit tests only
uv run tox -e lint                                                                 # Compile checks only
uv run tox -e unit -- -k "TestDeepMerge"                                           # Run specific tests
```

**Note on test output**: `monitor.summary()` now returns a `rich.Table` object. Tests use `Console.capture()` to render it to plain text for assertions. When adding new monitor tests, use the `_render()` helper pattern from `tests/test_monitor.py`.

**Run when touching executor, phases, or config schema:**

```bash
uv run sunbeam-deployer deploy --device-ip <IP> --phase host-setup -v   # Smoke test against real machine
```

This is expensive (requires a provisioned machine). Skip for doc-only or logging changes.

## Code Style

- **Python 3.14+**. Use `from __future__ import annotations` in every module.
- **80-char line length**. Enforced by `ruff` (configured in `pyproject.toml`).
- Type hints on all function signatures. Use `str | None`, not `Optional[str]`.
- Use `dataclasses` for all structured data.
- Never `print()`. Use `logging.getLogger("sunbeam_deployer.<module>")`.
- Imports sorted by `ruff` (isort rules): stdlib → third-party → local.
- Define `PHASE = "phase-name"` at module level in each phase file.
- Module-level docstring in every file. Function docstrings for public functions.
- Run `uv run ruff check --fix && uv run ruff format` before committing.

---

## Architecture

### Key Files

| File | Responsibility |
| ------ | --------------- |
| `__main__.py` | Thin entry point, delegates to `cli.py` |
| `cli.py` | rich-click group, commands (`deploy`, `list-jobs`), deploy logic, overrides |
| `config.py` | YAML loading, dataclass definitions, defaults, validation |
| `executor.py` | All command execution: local, host (SSH-transparent), LXD VM |
| `logger.py` | Dual logging — rich `LiveDisplay` (spinner + events) for terminal, plain text for file. Secret redaction. Module-level `update_spinner()`. No more `phase_logger`/`step_logger` adapters. |
| `monitor.py` | Phase/step status tracking, `rich.Table` summary with coloured status, error extraction |
| `phases/testflinger.py` | Phase 0: submit/attach TF job, poll, SSH setup |
| `phases/host_setup.py` | Phase 1: delegates to `scripts/host-setup.sh`, then parses Terraform outputs |
| `phases/vm_deploy.py` | Phase 2: per-VM snap install, prepare-node, manifest push (parallel) |
| `phases/cluster.py` | Phase 3: DNS validation, bootstrap, token-based join |
| `scripts/host-setup.sh` | Standalone bash script for Phase 1 (LXD, Terraform, repo, bootstrap) |

### Transparent SSH Routing

`run_host(cmd)` automatically wraps commands in SSH when a `RemoteTarget` is set. All phase code calls `run_host()` and works identically local or remote. Never bypass this — never use `subprocess` directly in phases.

- `run_local()` — always local. For `testflinger` CLI only. Never SSH-wrapped.
- `run_host()` — on the deployment host. SSH-wrapped when remote target is set.
- `run_in_vm(vm, cmd)` — inside LXD VM via `lxc exec`. Routes through `run_host()`.

### Data Flow Between Phases

```text
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

When running a single phase (e.g. `--phase cluster`), the tool reconstructs `InfraInfo` from existing Terraform outputs by re-running `_parse_terraform_outputs()`.

### Remote Path Handling (Critical)

**Never** call `os.path.expanduser()` or `Path.exists()` for remote paths. These check the local filesystem and return wrong results.

- Keep `~` unexpanded in config strings. The remote shell expands it.
- Use `run_host("test -f /path")` for remote file existence checks.
- Use `cd ~/path && command` instead of `command -chdir=~/path` (bash doesn't expand `~` inside flag values).

### Terraform as Source of Truth

VM metadata comes from `terraform output -json compute_nodes`, not generated YAML files. Add new VM attributes to `host_setup._parse_terraform_outputs()`.

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

### Role Priority

Per-node roles: `config.sunbeam.node_roles[name]` > `config.sunbeam.roles` (global default) > Terraform output `roles`.

### Cluster Node Count

`config.sunbeam.cluster_node_count` controls how many VMs participate in the Sunbeam cluster:

- `0` (default): all Terraform VMs join the cluster.
- `1`: single-node cluster — bootstrap only, no join, DNS validation skipped.
- `N`: first *N* VMs (in Terraform output order) join; remaining VMs are still deployed (snap installed, prepared) but excluded from bootstrap/join.

### Concurrency

- Phase 2 (VM deploy): `ThreadPoolExecutor`, max workers from `config.concurrency.vm_deploy`. All VMs are deployed regardless of `cluster_node_count`.
- Phase 3 (Cluster): Strictly sequential. Bootstrap first, then join one node at a time. Never parallelize joins.

### Sunbeam CLI Syntax

These use **positional** arguments, not flags:

```bash
sunbeam cluster add --format yaml <FQDN>       # NOT --name <FQDN>
sunbeam cluster join --role <roles> <TOKEN>    # NOT --token <TOKEN>
```

### Secret Redaction

The logger automatically redacts sensitive patterns from both file and terminal logs:

- `token: <value>` → `token: <REDACTED>`
- SSH private keys → `<REDACTED>`
- `apikey: <value>` → `apikey: <REDACTED>`
- `password: <value>` → `password: <REDACTED>`

---

## Elevated Care Zones

Edit these with extra caution — bugs here caused real deployment failures:

| File / Function | Risk | Reason |
| ---------------- | ------ | -------- |
| `executor.py` — `run_host()`, `_run_via_ssh()` | **High** | All remote execution routes through here. Breaking SSH routing breaks everything. |
| `host_setup.py` — `_run_host_setup_script()`, `_parse_terraform_outputs()` | **High** | `_run_host_setup_script()` pipes the standalone bash script with env vars (REPO_DIR must NOT be quoted for `~` expansion). `_parse_terraform_outputs()` is source of truth for VM metadata. Wrong parsing cascades to Phase 2 and 3. |
| `cluster.py` — `_extract_token()`, `_join_node()`, `_resolve_cluster_nodes()` | **High** | Token parsing and join syntax are fragile. Positional arg order matters. Node filtering logic determines cluster topology. Error messages are kept brief (stdout omitted) — full output is in the log file. |
| `config.py` — `_deep_merge()`, path handling | **Medium** | `~` must stay unexpanded for `repo_dir`. `_deep_merge` must not clobber nested keys. |
| `vm_deploy.py` — `_push_manifest()` | **Medium** | Must use `run_host("test -f ...")` not `os.path.exists()` for remote checks. |
| `logger.py` — `LiveDisplay`, `_LiveHandler` | **Medium** | Live display routes log events through a `deque`-backed render. The `_LiveHandler` is injected as a terminal handler. When verbose, no `LiveDisplay` is created — falls back to `RichHandler`. |
| `monitor.py` — `summary()`, `_extract_error_lines()` | **Medium** | `summary()` now returns a `rich.Table` (not a string). `_extract_error_lines()` filters stdout for diagnostic keywords; if none found, keeps last 3 lines (max 5). |

## Decision Guidelines

### Do Without Asking

- Bug fixes with clear root cause
- Adding logging, error messages, or retries
- Config validation rules
- Making operations idempotent (check-before-act pattern)
- Updating comments and docstrings

### Ask First

- Adding new phases or changing phase order
- Modifying the executor's SSH routing logic
- Changing Terraform output parsing structure
- Adding new dependencies
- Changing the config schema (adding/removing/renaming keys)
- Changing `sunbeam` CLI command syntax or argument order
- Changing concurrency model (sequential ↔ parallel)

### Never Do

- Use `os.path.expanduser()` or `Path().exists()` for remote paths
- Use `subprocess` directly in phase code — always use `executor` functions
- Call `print()` — use the logger
- Hardcode VM names or IPs — always use Terraform outputs
- Skip idempotency checks — always check state before acting
- Parallelize cluster join operations
- Mix unrelated refactors with bug fixes in the same commit

## Common Pitfalls

These are real bugs discovered during live testing:

1. **`~` expansion**: `os.path.expanduser("~")` returns the LOCAL home, not remote. Use `run_host("test -d ~/dir")`.
2. **`~` in single quotes**: `shlex.quote("~/dir")` produces `'~/dir'` — single quotes block `~` expansion. Pass `REPO_DIR` bare (no quoting) so the remote bash expands it: `f"REPO_DIR={cfg.repo_dir}"` instead of `f"REPO_DIR={shlex.quote(cfg.repo_dir)}"`.
3. **`-chdir=~/path`**: Bash doesn't expand `~` inside flag values. Use `cd ~/path && terraform output`.
4. **LXD init**: `bootstrap.sh`'s preseed fails on block devices. Always pre-init with `lxd init --auto`.
5. **Snap idempotency**: `snap install` fails if already installed. Check `snap list openstack` first.
6. **Local vs remote FS**: `os.path.exists()` checks local FS. Use `run_host("test -f ...")` for remote.
7. **`newgrp snap_daemon`**: Cannot run in automation (starts subshell). Fresh `lxc exec` sessions inherit the group.
8. **Cluster syntax**: NAME and TOKEN are positional args, not flags. See "Sunbeam CLI Syntax" above.
9. **Monitor phase name mismatch**: When running `--phase cluster`, use `HS_PHASE` for reconstruction monitor.

## Git Workflow

- Branch naming: `<type>/<short-description>` (e.g. `fix/cluster-join-syntax`, `feat/maas-support`)
- Commit messages: imperative mood, concise first line. Detail in body if needed.
- Always include: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- Keep changes scoped. Do not mix refactors with fixes.
- Run all verification checks before committing.
- Summarize what was validated in PR descriptions.

---

## Configuration

### Configuration File

The tool reads a YAML configuration file. If no config file is provided, built-in defaults are used.

```bash
cp config.example.yaml config.yaml
```

### Key Configuration Sections

```yaml
testflinger:
  enabled: true                    # Enable Testflinger provisioning
  queue: openstack                 # Testflinger queue name
  distro: noble                    # Ubuntu version to provision
  reserve_timeout: 259200          # 3 days reservation
  ssh_keys:                        # Keys for reserve access
    - lp:himax16
    - gh:himax16
  ssh_user: ubuntu                 # SSH user on provisioned machine
  provision_timeout: 1800          # Max wait for provisioning

snap:
  source: store                    # "store" or "local"
  channel: 2024.1/edge             # Snap store channel
  # local_path: ~/openstack.snap  # Path for local installs
  # install_method: dangerous      # "dangerous" or "try"

sunbeam:
  roles: [control, compute, storage]  # Default roles for all nodes
  node_roles:                          # Per-node role overrides
    bm0: [control, compute, storage]
    bm1: [compute, storage]
    bm2: [compute]
  manifest: true                   # Push Terraform manifest to VMs
  accept_defaults: false           # Use --accept-defaults on bootstrap
  cluster_node_count: 0            # 0 = all nodes; N = first N nodes

timeouts:
  cloud_init_wait: 600     # VM cloud-init (10 min)
  snap_install: 600        # Snap installation (10 min)
  prepare_node: 600        # prepare-node-script (10 min)
  cluster_bootstrap: 7200  # Bootstrap (2 hours — typically 30-60 min)
  cluster_join: 3600       # Join per node (1 hour — typically 40-45 min)
  terraform_apply: 3600    # Terraform apply (1 hour)

concurrency:
  vm_deploy: 2    # Max VMs deployed in parallel

terraform:
  extra_args: []            # Extra args passed to bootstrap.sh
  bootstrap_retries: 1      # Auto-retry on VM boot timeout (0 = no retry)
  vm_boot_timeout: 15m       # Per-VM boot timeout (LXD/MAAS provider)
```

**Terraform LXD provider quirk**: The `timeouts { create }` attribute on `lxd_instance` *is* parsed by `Create()`, but `startInstance()` eventually calls `waitForState()` which has a **hardcoded 3-minute timeout**: `Timeout: 3 * time.Minute`. This means every VM start attempt always fails after exactly 3 minutes if the VM hasn't fully left `Running (initializing)` state, regardless of the configured value. `bootstrap_retries` works around this — the second apply finds the VMs already running and passes immediately. The `host-setup.sh` script also runs `terraform untaint` between retries.

CLI flags always take priority over config file values (see [CLI Reference](#complete-cli-reference)).

---

## Run & Monitor

### Quick Reference

| Task | Command |
| ------ | --------- |
| Full deploy (new TF job) | `uv run sunbeam-deployer deploy --testflinger -c config.yaml` |
| Attach to existing job | `uv run sunbeam-deployer deploy --tf-job-id <UUID>` |
| Direct SSH deploy | `uv run sunbeam-deployer deploy --device-ip <IP>` |
| Re-run single phase | `uv run sunbeam-deployer deploy --device-ip <IP> --phase <phase>` |
| Verbose output | Add `-v` to any command |
| Auto-cancel TF job on failure | Add `--cancel-on-failure` |
| List active TF jobs | `uv run sunbeam-deployer list-jobs` |
| List all TF jobs (JSON) | `uv run sunbeam-deployer list-jobs --all --format json` |

Phases run in order: `testflinger` (~5-30 min) → `host-setup` (~5 min) → `vm-deploy` (~5 min) → `cluster` (~1.5 hr). Total: ~1.5-2 hours for 3 nodes.

### Deployment Modes

| Mode | Command | When |
| ------ | --------- | ------ |
| New Testflinger job | `uv run sunbeam-deployer deploy --testflinger -c config.yaml` | Provision fresh machine |
| Attach to existing job | `uv run sunbeam-deployer deploy --tf-job-id <UUID>` | Job already in reserve state |
| Direct SSH | `uv run sunbeam-deployer deploy --device-ip 10.241.2.45` | Skip Testflinger entirely |
| Local machine | `uv run sunbeam-deployer deploy` | Machine running the tool IS the target |

```bash
uv run sunbeam-deployer deploy --device-ip 10.241.2.45 --phase host-setup
uv run sunbeam-deployer deploy --device-ip 10.241.2.45 --phase vm-deploy
uv run sunbeam-deployer deploy --device-ip 10.241.2.45 --phase cluster
```

When running `--phase vm-deploy` or `--phase cluster`, the tool automatically reconstructs VM metadata from existing Terraform outputs. The infrastructure must already be set up.

### Real-Time Terminal Output

The deployer shows a **spinner** with the current phase/step and a scrolling tail of recent log events:

```log
⠋ Phase: host-setup — Install and initialise LXD
  16:32:01  ━━━ Phase: host-setup ━━━
  16:32:01    ▸ Install and initialise LXD
  16:32:05    ▸ Install Terraform snap
```

In **verbose mode** (`-v`), the live display is replaced by a `RichHandler` that shows every log line with timestamps and rich tracebacks.

**Status symbols**: ✅ success, ❌ failed, ⏳ pending, 🔄 running, ⏭️ skipped

### Log Files

Full debug logs are written to `~/.local/share/sunbeam-deployer/logs/sunbeam-deploy-YYYYMMDD-HHMMSS.log`. The log file includes every command executed, every line of output (prefixed with ` | `), timing information, and SSH connection details (secrets redacted). The log file path is also shown in the deployment summary footer.

**For LLM agents monitoring deployments**: Start the deployment with `-v` to get full terminal output. If not using `-v`, the live display (default, non-verbose) shows a rolling 8-line event tail — not every log line. Parse the log file for the complete picture:

```bash
# Option 1: Start with verbose to see all output
uv run sunbeam-deployer deploy --device-ip <IP> --phase <phase> -v

# Option 2: Parse the log file (always has full output)
LOG=$(ls -t ~/.local/share/sunbeam-deployer/logs/sunbeam-deploy-*.log | head -1)
grep "FAILED\|ERROR" "$LOG" | tail -20
```

```bash
LOG=$(ls -t ~/.local/share/sunbeam-deployer/logs/sunbeam-deploy-*.log | head -1)    # Find latest log
tail -f "$LOG"                                       # Follow real-time
grep "━━━ Phase:" "$LOG" | tail -1                   # Current phase
grep -c "FAILED\|ERROR" "$LOG"                       # Error count
testflinger status <UUID>                            # TF job phase
```

### Deployment Summary

At the end of each run (success or failure), a summary table prints with ✅/❌ per phase/step, durations, and overall status.

### Post-Deployment Job Handling

- **Submitted jobs** (`--testflinger` or `--tf-job-file`): After success, prompts `Cancel Testflinger job <UUID> and release the machine? [y/N]`. Default is No (keep alive).
- **Attached jobs** (`--tf-job-id`): No prompt — logs the job ID and how to cancel manually.
- **`--cancel-on-failure`**: Auto-cancels on failure without prompting.
- Manual cancel: `testflinger cancel <UUID>`. Jobs expire after `reserve_timeout` (default 3 days).

---

## Phase Reference

### Phase 0: Testflinger (`testflinger`)

**Purpose**: Provision a machine or connect to an existing one.

**Steps**: submit-job → wait-provision → ssh-connect

**Inputs**: Testflinger config (queue, distro, SSH keys)
**Outputs**: SSH connection to the provisioned machine

**Testflinger Job Phases**: `setup → provision → firmware_update → test → allocate → reserve → cleanup`

The tool waits for `reserve` (or `test`) before proceeding.

- `testflinger submit --quiet <file>` returns just the job UUID
- `testflinger status <id>` returns the current phase name
- `testflinger results <id>` returns JSON with `device_info.device_ip`
- Polling starts at 15s intervals, slows to 30s after 5 minutes

### Phase 1: Host Setup (`host-setup`)

**Purpose**: Install infrastructure tooling and create LXD VMs.

Phase 1 delegates to a **standalone bash script** (`scripts/host-setup.sh`) that can also be run independently on any Ubuntu machine. The Python tool pipes the script over SSH with environment variables from configuration.

**Steps** (inside `host-setup.sh`):

1. Install LXD snap (idempotent)
2. Initialize LXD with `lxd init --auto` (idempotent)
3. Install Terraform snap — classic confinement (idempotent)
4. Clone/pull repo — `himax16/sunbeam-proxified-dev` (idempotent)
5. Run bootstrap.sh — Terraform init/apply, creates VMs + networks

After the script completes, Python re-runs `_parse_terraform_outputs()` to read `terraform output -json compute_nodes` and `network_topology`.

**Outputs**: `InfraInfo` containing `ComputeNode` objects (name, fqdn, hostname, ip, roles, osd_devices), paths to manifest.yaml, ssh_private_key, plan directory, and management domain name.

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
- `newgrp snap_daemon` is NOT called in automation; fresh `lxc exec` sessions inherit the group from the login shell
- Manifest is pushed via `lxc file push` on the host (not local filesystem)

### Phase 3: Cluster (`cluster`)

**Purpose**: Bootstrap the Sunbeam cluster and join additional nodes. Only nodes selected by `cluster_node_count` participate (default: all).

**Steps**:

1. **Resolve cluster nodes**: Select the first *N* VMs (or all when count is 0)
2. **dns-validation**: Verify every cluster node can `getent hosts` every other cluster node's FQDN (skipped for single-node clusters)
3. **bootstrap-bm0**: Run `sunbeam -v cluster bootstrap` on the first selected node
4. **join-bm1, join-bm2, ...**: Generate token on bm0, join each remaining node sequentially

**Key Details**:

- **Token generation**: `sunbeam cluster add --format yaml <FQDN>` — `<FQDN>` is positional
- **Token extraction**: Parses YAML `token:` field or falls back to bare base64 regex
- **Join command**: `sunbeam -v cluster join --role <roles> <TOKEN>` — `<TOKEN>` is positional
- Join is sequential (one node at a time) to avoid cluster races
- Bootstrap typically takes 30-60 minutes; joins take 40-45 minutes each
- **Single-node cluster**: When `cluster_node_count: 1`, DNS validation and join steps are skipped entirely; only bootstrap runs
- Non-cluster VMs (excluded by count) still have the snap installed and are prepared in Phase 2, enabling later manual expansion

---

## Troubleshooting & Error Recovery

### General Approach

1. **Read the log file** — it contains every command and output:

    ```bash
    LOG=$(ls -t ~/.local/share/sunbeam-deployer/logs/sunbeam-deploy-*.log | head -1)
    grep "FAILED\|ERROR\|error\|failed" "$LOG" | tail -20
    ```

2. **Check the summary** — identify which phase/step failed

3. **Re-run the failed phase** using `deploy --phase <name>`:

    ```bash
    uv run sunbeam-deployer deploy --device-ip <IP> --phase <failed-phase>
    ```

4. **Check Sunbeam logs inside the VM** (for cluster phase failures):

    ```bash
    ssh ubuntu@<device-ip>
    lxc exec bm0 -- cat /home/ubuntu/snap/openstack/common/logs/sunbeam.log
    ```

### Phase-Specific Recovery

| Symptom | Action |
| --------- | -------- |
| LXD install fails | SSH in, check `snap list lxd`, try `sudo snap install lxd` manually |
| Terraform apply fails | SSH in, `cd ~/sunbeam-proxified-dev/manual-infra && terraform plan` to diagnose |
| bootstrap.sh fails | Check if LXD is initialized: `lxc storage show default` |
| Repo clone fails | Check network, try `git clone <url>` manually |
| VM not responsive | `lxc list` to check VM state, `lxc start bm0` if stopped |
| cloud-init timeout | `lxc exec bm0 -- cloud-init status` to check |
| snap install fails | `lxc exec bm0 -- sudo snap install openstack --channel=2024.1/edge` manually |
| prepare-node fails | Check snap connections: `lxc exec bm0 -- sudo snap connections openstack` |
| DNS validation fails | Check DNS container: `lxc exec dns -- cat /etc/dnsmasq.d/*.conf` |
| Bootstrap timeout | Check sunbeam logs: `lxc exec bm0 -- cat /home/ubuntu/snap/openstack/common/logs/sunbeam.log` |
| Token generation fails | Verify bootstrap succeeded: `lxc exec bm0 -- sudo -iu ubuntu sunbeam cluster list` |
| Join fails | Check the token is valid, verify DNS from the joining node |

### Manual Intervention via SSH

```bash
ssh ubuntu@<device-ip>
lxc list
lxc exec bm0 -- sudo -iu ubuntu bash
# Inside bm0:
sunbeam cluster list
snap list openstack
snap services openstack
tail -100 ~/snap/openstack/common/logs/sunbeam.log
```

---

## Post-Deployment Verification

After a successful deployment, verify the cluster is healthy:

```bash
ssh ubuntu@<device-ip>
lxc exec bm0 -- sudo -iu ubuntu bash
```

| Check | Command (inside bm0) | Expected |
| ------- | --------------------- | ---------- |
| Cluster members | `sunbeam cluster list` | All nodes "running", roles "active" |
| Juju models | `juju models` | Models created and active |
| Services | `juju status` | All units active/idle |

Expected output for a 3-node cluster:

```log
Name    Status   Control  Compute  Storage
bm0.res running  active   active   active
bm1.res running  active   active   active
bm2.res running  active   active   active
```

---

## Testflinger Job Management

### Job Lifecycle

```text
Submit → Queued → Setup → Provision → Reserve → (Work) → Cancel/Cleanup
```

### Common Operations

```bash
testflinger submit --quiet <job-file.yaml>    # Returns: <job-uuid>
testflinger status <job-uuid>                 # Returns: reserve
testflinger results <job-uuid>                # JSON with device_info.device_ip, agent_name
testflinger cancel <job-uuid>                 # Release the machine
testflinger poll <job-uuid>                   # Stream output to terminal
```

### Auto-Generated Job YAML

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

---

## Timing Reference

Based on real deployment data (3-node cluster on Testflinger):

| Operation | Typical Duration | Timeout Default |
| ----------- | ----------------- | ----------------- |
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

---

## Complete CLI Reference

```bash
sunbeam-deployer [--version] COMMAND [ARGS]...

Commands:
  deploy              Deploy Sunbeam
  list-jobs           List Testflinger jobs and their IP addresses

sunbeam-deployer deploy [OPTIONS]

General options:
  -c, --config FILE          YAML configuration file
  -v, --verbose              DEBUG-level terminal output
  --phase PHASE              Run specific phase:
                               all (default), testflinger, host-setup,
                               vm-deploy, cluster

Testflinger options:
  --testflinger              Enable Testflinger provisioning
  --tf-job-id JOB_ID         Attach to existing job UUID
  --tf-job-file FILE         Submit this job YAML
  --tf-ssh-key PATH          SSH private key for the machine
  --device-ip IP             Skip Testflinger, SSH directly to this IP

Snap and deployment overrides:
  --snap-channel CHANNEL     Override snap channel (sets source=store)
  --snap-revision REV        Pin snap revision (sets source=store)
  --snap-file PATH           Install from local .snap file (sets source=local)
  --deploy-mode MODE         Override deploy mode: manual or maas
  --repo-dir DIR             Override repo clone directory

Behaviour flags:
  --accept-defaults          Pass --accept-defaults to sunbeam bootstrap
  --no-manifest              Skip pushing manifest.yaml to VMs
  --tf-arg ARG               Extra terraform arg (repeatable)
  --cancel-on-failure        Auto-cancel Testflinger job if deployment fails

sunbeam-deployer list-jobs [OPTIONS]

Options:
  -v, --verbose              Enable verbose output
  -a, --all                  Show all jobs (default: active/reserve only)
  -f, --format FORMAT        Output format: table (default) or json

Global:
  --version                  Show version and exit
  --help                     Show help and exit
```

### Exit Codes

| Code | Meaning |
| ------ | --------- |
| 0 | Deployment completed successfully |
| 1 | Deployment failed (config error, phase failure, or unhandled exception) |

---

## Decision Trees

### Which Deployment Mode to Use?

```text
Do you have a Testflinger job UUID?
├── Yes → uv run sunbeam-deployer deploy --tf-job-id <UUID>
└── No
    └── Do you want to provision a new machine?
        ├── Yes → uv run sunbeam-deployer deploy --testflinger
        └── No
            └── Do you have a machine IP?
                ├── Yes → uv run sunbeam-deployer deploy --device-ip <IP>
                └── No → uv run sunbeam-deployer deploy (runs locally)
```

### How to Resume After Failure?

```text
Which phase failed?
├── testflinger → Fix network/queue issues, re-run deploy --testflinger
├── host-setup
│   ├── LXD issue → SSH in, fix manually, re-run deploy --phase host-setup
│   ├── Terraform issue → SSH in, cd to plan dir, debug terraform
│   └── bootstrap.sh → Check LXD init, re-run deploy --phase host-setup
├── vm-deploy
│   ├── Single VM → Fix the VM, re-run deploy --phase vm-deploy (idempotent)
│   └── All VMs → Check network, snap store access
└── cluster
    ├── DNS validation → Fix DNS container, re-run deploy --phase cluster
    ├── Bootstrap → Check sunbeam logs in bm0, re-run deploy --phase cluster
    └── Join → Check token, DNS, re-run deploy --phase cluster
```

### Choosing Snap Source

```text
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
| ------ | --------- | ------- |
| **Shell/Bash execution** | Run CLI commands | `uv run sunbeam-deployer ...`, `ssh`, `testflinger` |
| **File read** | Read config files and logs | Read `config.yaml`, `~/.local/share/sunbeam-deployer/logs/*.log` |
| **SSH** | Connect to remote machines for debugging | `ssh ubuntu@<ip>` |

### Minimum Agent Capabilities

1. **Execute shell commands** and read their output (stdout + stderr + exit code)
2. **Read files** (config files, log files) for monitoring and debugging
3. **Write files** (create config.yaml from template)
4. **Handle long-running processes** — deployments take 1.5-2 hours
5. **Parse structured output** (JSON from testflinger results, deployment summary)
6. **Pattern matching** on log output to identify errors

### Suggested Agent Workflow

```text
1. Verify prerequisites (uv, testflinger CLI, SSH access)
2. Create/validate config.yaml
3. Start deployment (uv run sunbeam-deployer ...)
4. Monitor progress (choose one):
   a. Start with -v for full terminal output, OR
   b. Tail the log file periodically
5. Check for error patterns in the log file
6. If stuck, check testflinger job status
7. On completion:
   a. Parse the summary table for success/failure
   b. If success: run verification checks
   c. If failure: identify failed phase, attempt recovery
8. Handle post-deployment cancel prompt:
   - If the job was submitted by the tool, it will prompt to cancel.
   - Answer "y" to release the machine, or "N" to keep it alive.
   - For attached jobs (--tf-job-id), cancel manually when done.
9. Report results with:
   - Overall status (success/failure)
   - Duration per phase
   - Any errors encountered
   - Testflinger job ID (for future reference)
   - Device IP (for SSH access)
```
