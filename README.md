# Sunbeam Deploy Bot

Automated deployment of [Sunbeam](https://microstack.run/) (Canonical OpenStack)
on Testflinger machines using LXD VMs and Terraform.

## Quick Start

```bash
# Install dependencies using uv
uv sync
```

```bash
# Copy and edit config
cp config.example.yaml config.yaml
nano config.yaml
```

```bash
# Run full deployment
uv run sunbeam-deployer -c config.yaml
```

```bash
# Or with defaults (no config file needed)
uv run sunbeam-deployer
```

## How It Works

The deployer runs four phases sequentially:

### Phase 0: Testflinger (optional)

- Submits a Testflinger job (or attaches to an existing one via `--tf-job-id`)
- Polls until the machine is provisioned and in `reserve` state
- Extracts the device IP from results
- Establishes SSH and configures all subsequent phases to execute remotely
- Alternatively, `--device-ip` skips Testflinger and connects directly via SSH

### Phase 1: Host Setup

Phase 1 delegates to a **standalone bash script** (`scripts/host-setup.sh`) that
can also be run independently on any Ubuntu machine:

```bash
# Standalone — run directly on the target machine
wget -O - https://raw.githubusercontent.com/.../host-setup.sh | bash

# With custom options
REPO_URL=https://... REPO_BRANCH=dev DEPLOY_MODE=maas bash host-setup.sh
```

When run via `sunbeam-deployer`, the Python tool pipes the bundled script over
SSH and exports the appropriate environment variables from configuration.

The script does the following:

- Installs **LXD** and **Terraform** snaps (if not present)
- Clones the [sunbeam-proxified-dev](https://github.com/himax16/sunbeam-proxified-dev) repo
- Runs `bootstrap.sh` which:
  - Initialises LXD with the largest available disk
  - Creates LXD networks (management + compute bridges)
  - Creates LXD VMs (`bm0`, `bm1`, …) via Terraform
  - Generates `manifest.yaml` and `testbed.yaml`

After the script completes, Python parses Terraform outputs (`compute_nodes`,
`network_topology`) for VM metadata.

### Phase 2: VM Deployment

For each VM (bounded parallelism, default 2):

- Waits for the VM agent + cloud-init to finish
- Installs the **openstack** snap (from store or local `.snap` file)
- Sets up the `sunbeam` snap alias
- Connects snap interfaces (when installing from local build)
- Runs `sunbeam prepare-node-script --bootstrap`
- Pushes the Terraform-generated `manifest.yaml`

### Phase 3: Cluster Lifecycle

- **Validates DNS** — checks that every node can resolve every other node's FQDN
- **Bootstraps** the cluster on the first node (`bm0`)
- **Joins** each remaining node sequentially, generating a join token per node

## CLI Usage

The CLI has two subcommands: `deploy` (the default) and `list-jobs`.

### `deploy` — Run a Sunbeam deployment

```bash
sunbeam-deployer deploy [-c CONFIG] [-v] [--phase PHASE] [options]
```

When no subcommand is given, `deploy` is implied for backward compatibility:

```bash
# These are equivalent:
uv run sunbeam-deployer --device-ip 10.241.5.22
uv run sunbeam-deployer deploy --device-ip 10.241.5.22
```

**Phases:**

| Flag | Description |
| ---- | ----------- |
| `--phase all` | Run all phases (default) |
| `--phase testflinger` | Only provision via Testflinger |
| `--phase host-setup` | Only run infrastructure setup |
| `--phase vm-deploy` | Only deploy snaps into VMs |
| `--phase cluster` | Only bootstrap/join the cluster |

**Testflinger options:**

| Flag | Description |
| ---- | ----------- |
| `--testflinger` | Enable Testflinger provisioning |
| `--tf-job-id JOB_ID` | Attach to an existing job |
| `--tf-job-file FILE` | Submit a specific job YAML |
| `--tf-ssh-key PATH` | SSH key for the Testflinger machine |
| `--device-ip IP` | Skip Testflinger, connect directly via SSH |

**General options:**

| Flag | Description |
| ---- | ----------- |
| `-c, --config FILE` | Path to YAML config file |
| `-v, --verbose` | Enable DEBUG-level terminal output |
| `--snap-channel CHANNEL` | Override snap channel (e.g. `2024.1/edge`) |
| `--snap-revision REV` | Pin to a specific snap revision |
| `--snap-file PATH` | Install from a local `.snap` file |
| `--deploy-mode MODE` | Override deploy mode: `manual` or `maas` |
| `--repo-dir DIR` | Override the repo clone directory |
| `--accept-defaults` | Pass `--accept-defaults` to sunbeam bootstrap |
| `--no-manifest` | Skip pushing `manifest.yaml` to VMs |
| `--tf-arg ARG` | Extra arg for terraform apply (repeatable) |
| `--cancel-on-failure` | Cancel Testflinger job on deployment failure |

### `list-jobs` — List Testflinger jobs

```bash
sunbeam-deployer list-jobs [-v] [-a] [-f table|json]
```

| Flag | Description |
| ---- | ----------- |
| `-a, --all` | Show all jobs (including waiting/completed); by default only active/reserve jobs |
| `-f, --format` | Output format: `table` (default) or `json` |
| `-v, --verbose` | Enable verbose (DEBUG) output |

**Examples:**

```bash
# Show only active/reserve jobs
uv run sunbeam-deployer list-jobs

# Show all jobs including waiting and completed
uv run sunbeam-deployer list-jobs --all

# Machine-readable JSON output
uv run sunbeam-deployer list-jobs --format json
```

### Examples

```bash
# Full deployment with defaults (local machine)
uv run sunbeam-deployer
```

```bash
# Submit a Testflinger job and deploy on it
uv run sunbeam-deployer --testflinger
```

```bash
# Submit using a custom job YAML
uv run sunbeam-deployer --tf-job-file ~/testflinger-job.yaml
```

```bash
# Attach to an already-provisioned Testflinger job
uv run sunbeam-deployer --tf-job-id 97c62a00-2522-414b-b296-1fe77a6bbd99
```

```bash
# Deploy on a machine you already have SSH access to
uv run sunbeam-deployer --device-ip 10.241.2.45
```

```bash
# Use a specific snap channel on a Testflinger machine
uv run sunbeam-deployer --testflinger --snap-channel 2024.2/beta
```

```bash
# Install from a local snap file
uv run sunbeam-deployer --snap-file ~/openstack_2024.1_amd64.snap
```

```bash
# Run only the cluster phase (infra already deployed)
uv run sunbeam-deployer --phase cluster
```

```bash
# Verbose output + custom terraform variables
uv run sunbeam-deployer -v --tf-arg="-var-file=custom.tfvars"
```

```bash
# Bootstrap with accept-defaults (no interactive prompts)
uv run sunbeam-deployer --accept-defaults
```

## Configuration

See [`config.example.yaml`](config.example.yaml) for the full configuration
reference with comments.

Key settings:

| Setting | Default | Description |
| --- | --- | --- |
| `testflinger.enabled` | `false` | Enable Testflinger provisioning |
| `testflinger.queue` | `openstack` | Testflinger queue name |
| `testflinger.distro` | `noble` | OS distro for provisioning |
| `testflinger.reserve_timeout` | `259200` | Reservation timeout (3 days) |
| `deploy_mode` | `manual` | `manual` (LXD-only) or `maas` |
| `snap.source` | `store` | `store` or `local` |
| `snap.channel` | `2026.1/edge` | Snap store channel |
| `snap.install_method` | `dangerous` | `dangerous` or `try` (for local installs) |
| `sunbeam.manifest` | `true` | Push Terraform-generated manifest to VMs |
| `concurrency.vm_deploy` | `2` | Max VMs deployed in parallel |
| `timeouts.cluster_bootstrap` | `7200` | Bootstrap timeout in seconds |

## Monitoring

The deployer provides dual monitoring:

- **Terminal**: Real-time coloured progress output with phase/step tracking
- **Log file**: Full debug-level logs in `./logs/sunbeam-deploy-YYYYMMDD-HHMMSS.log`

At the end of each run, a summary table shows every phase and step with
status (✅/❌) and duration.

Secrets (tokens, private keys, passwords) are automatically redacted from logs.

## Running Individual Phases

You can re-run a specific phase if a previous run partially succeeded:

```bash
# Re-run only VM deployment (after host-setup already succeeded)
uv run sunbeam-deployer -c config.yaml --phase vm-deploy
```

```bash
# Re-run only cluster bootstrap/join
uv run sunbeam-deployer -c config.yaml --phase cluster
```

When running `vm-deploy` or `cluster` standalone, the deployer reconstructs
VM metadata from existing Terraform outputs.

## Development

Development dependencies (pytest, ruff, tox) are **dev-only** — they are not
installed for production use. Install them with:

```bash
# Installs runtime + dev deps
uv sync
```

### Running Checks with tox

[tox](https://tox.wiki/) is used to run all checks in isolated environments.
Configuration lives in [`tox.ini`](tox.ini).

| Command | Description |
| --- | --- |
| `uv run tox` | Run **all** environments (unit + lint) |
| `uv run tox -e unit` | Run unit tests only |
| `uv run tox -e lint` | Run ruff linter + formatter check only |
| `uv run tox -e unit -- -k "TestDeepMerge"` | Run specific tests |
| `uv run tox -e unit -- tests/test_config.py` | Run a single test file |

#### tox Environments

- **`unit`** — Runs `pytest tests/ -v`. Supports `{posargs}` for passing
  extra arguments (e.g. `-k`, `--tb=short`, specific files).
- **`lint`** — Runs `ruff check` and `ruff format --check` against
  `sunbeam_deployer/` and `tests/`. Enforces 80-char line length, import
  sorting, and standard Python linting rules (E/F/W/I/UP/B/SIM).

#### Fixing Lint Issues

```bash
uv run ruff check --fix sunbeam_deployer/ tests/    # Auto-fix lint errors
uv run ruff format sunbeam_deployer/ tests/         # Auto-format code
```

## Project Structure

```text
sunbeam_deployer/
├── __init__.py          # Package metadata
├── __main__.py          # CLI entry point (argparse, dispatch)
├── commands.py          # Subcommand handlers (list-jobs)
├── config.py            # Configuration loading + validation
├── executor.py          # Command execution (host + LXD VMs)
├── logger.py            # Dual file/terminal logging with secret redaction
├── monitor.py           # Phase/step status tracking + summary
├── phases/
│   ├── testflinger.py   # Phase 0: Testflinger job submission + SSH setup
│   ├── host_setup.py    # Phase 1: LXD + Terraform infrastructure
│   ├── vm_deploy.py     # Phase 2: Snap install + node preparation
│   └── cluster.py       # Phase 3: Bootstrap + join
└── scripts/
    └── host-setup.sh    # Standalone bash script for Phase 1
```

## Prerequisites

The deployer should be run on a machine with:

- **[uv](https://docs.astral.sh/uv/)** (Python project manager)
- Ubuntu 22.04+ (or equivalent with `snap` support)
- `sudo` access (for snap installation)
- Network access to the snap store and GitHub
- Sufficient resources for LXD VMs (see `config.example.yaml` for defaults)
