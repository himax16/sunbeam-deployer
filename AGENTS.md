# AGENTS.md

## Overview

Automated deployment of [Canonical Sunbeam](https://microstack.run/) (OpenStack) on [Testflinger](https://canonical-testflinger.readthedocs-hosted.com/latest/) bare-metal machines. Python CLI managed by [uv](https://docs.astral.sh/uv/) + [hatchling](https://hatch.pypa.io/). Only runtime dependency is `pyyaml`.

## Dev Environment

```bash
uv sync                              # Install dependencies
uv run sunbeam-deployer --help        # Validate CLI loads
uv run sunbeam-deployer --version     # Expect: sunbeam-deployer 0.1.0
```

Add new dependencies to `pyproject.toml` under `[project] dependencies`, then `uv sync`.

**Build note**: Project name is `sunbeam-deploy-bot`, package dir is `sunbeam_deployer`. The mapping lives in `[tool.hatch.build.targets.wheel] packages = ["sunbeam_deployer"]`. Do not rename either without updating this.

## Testing & Verification

Testing is managed via [tox](https://tox.wiki/) with config in `tox.ini`. Two environments: `unit` (pytest) and `lint` (ruff check + format).

**Always run after any change:**

```bash
uv run python -c "from sunbeam_deployer import __version__; print(__version__)"   # Import check
uv run sunbeam-deployer --help                                                     # CLI loads
uv run python -c "from sunbeam_deployer.config import load_config; load_config()"  # Config parses
uv run tox                                                                         # All checks (unit + lint)
uv run tox -e unit                                                                 # Unit tests only
uv run tox -e lint                                                                 # Compile checks only
uv run tox -e unit -- -k "TestDeepMerge"                                           # Run specific tests
```

**Run when touching executor, phases, or config schema:**

```bash
uv run sunbeam-deployer --device-ip <IP> --phase host-setup -v   # Smoke test against real machine
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

## Architecture

### Key Files

| File | Responsibility |
|------|---------------|
| `__main__.py` | CLI entry point, phase orchestration, argparse |
| `config.py` | YAML loading, dataclass definitions, defaults, validation |
| `executor.py` | All command execution: local, host (SSH-transparent), LXD VM |
| `monitor.py` | Phase/step status tracking, summary table rendering |
| `phases/testflinger.py` | Phase 0: submit/attach TF job, poll, SSH setup |
| `phases/host_setup.py` | Phase 1: LXD, Terraform, clone repo, bootstrap.sh, parse outputs |
| `phases/vm_deploy.py` | Phase 2: per-VM snap install, prepare-node, manifest push (parallel) |
| `phases/cluster.py` | Phase 3: DNS validation, bootstrap, token-based join |

### Transparent SSH Routing

`run_host(cmd)` automatically wraps commands in SSH when a `RemoteTarget` is set. All phase code calls `run_host()` and works identically local or remote. Never bypass this — never use `subprocess` directly in phases.

- `run_local()` — always local. For `testflinger` CLI only. Never SSH-wrapped.
- `run_host()` — on the deployment host. SSH-wrapped when remote target is set.
- `run_in_vm(vm, cmd)` — inside LXD VM via `lxc exec`. Routes through `run_host()`.

### Remote Path Handling (Critical)

**Never** call `os.path.expanduser()` or `Path.exists()` for remote paths. These check the local filesystem and return wrong results.

- Keep `~` unexpanded in config strings. The remote shell expands it.
- Use `run_host("test -f /path")` for remote file existence checks.
- Use `cd ~/path && command` instead of `command -chdir=~/path` (bash doesn't expand `~` inside flag values).

### Terraform as Source of Truth

VM metadata comes from `terraform output -json compute_nodes`, not generated YAML files. Add new VM attributes to `host_setup._parse_terraform_outputs()`.

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

```
sunbeam cluster add --format yaml <FQDN>              # NOT --name <FQDN>
sunbeam cluster join --role <roles> <TOKEN>             # NOT --token <TOKEN>
```

## Elevated Care Zones

Edit these with extra caution — bugs here caused real deployment failures:

| File / Function | Risk | Reason |
|----------------|------|--------|
| `executor.py` — `run_host()`, `_run_via_ssh()` | **High** | All remote execution routes through here. Breaking SSH routing breaks everything. |
| `host_setup.py` — `_parse_terraform_outputs()` | **High** | Source of truth for VM metadata. Wrong parsing cascades to Phase 2 and 3. |
| `cluster.py` — `_extract_token()`, `_join_node()`, `_resolve_cluster_nodes()` | **High** | Token parsing and join syntax are fragile. Positional arg order matters. Node filtering logic determines cluster topology. |
| `config.py` — `_deep_merge()`, path handling | **Medium** | `~` must stay unexpanded for `repo_dir`. `_deep_merge` must not clobber nested keys. |
| `vm_deploy.py` — `_push_manifest()` | **Medium** | Must use `run_host("test -f ...")` not `os.path.exists()` for remote checks. |

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

1. **`~` expansion**: `os.path.expanduser("~")` returns the LOCAL home, not remote. Use `run_host("test -d ~/dir")`.
2. **`-chdir=~/path`**: Bash doesn't expand `~` inside flag values. Use `cd ~/path && terraform output`.
3. **LXD init**: `bootstrap.sh`'s preseed fails on block devices. Always pre-init with `lxd init --auto`.
4. **Snap idempotency**: `snap install` fails if already installed. Check `snap list openstack` first.
5. **Local vs remote FS**: `os.path.exists()` checks local FS. Use `run_host("test -f ...")` for remote.
6. **`newgrp snap_daemon`**: Cannot run in automation (starts subshell). Fresh `lxc exec` sessions inherit the group.
7. **Cluster syntax**: NAME and TOKEN are positional args, not flags. See "Sunbeam CLI Syntax" above.

## Git Workflow

- Branch naming: `<type>/<short-description>` (e.g. `fix/cluster-join-syntax`, `feat/maas-support`)
- Commit messages: imperative mood, concise first line. Detail in body if needed.
- Always include: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- Keep changes scoped. Do not mix refactors with fixes.
- Run all verification checks before committing.
- Summarize what was validated in PR descriptions.

## Run & Monitor

### Quick Reference

| Task | Command |
|------|---------|
| Full deploy (new TF job) | `uv run sunbeam-deployer --testflinger -c config.yaml` |
| Attach to existing job | `uv run sunbeam-deployer --tf-job-id <UUID>` |
| Direct SSH deploy | `uv run sunbeam-deployer --device-ip <IP>` |
| Re-run single phase | `uv run sunbeam-deployer --device-ip <IP> --phase <phase>` |
| Verbose output | Add `-v` to any command |
| Auto-cancel TF job on failure | Add `--cancel-on-failure` |

Phases run in order: `testflinger` (~5-30 min) → `host-setup` (~5 min) → `vm-deploy` (~5 min) → `cluster` (~1.5 hr). Total: ~1.5-2 hours for 3 nodes.

### Monitoring

```bash
LOG=$(ls -t logs/sunbeam-deploy-*.log | head -1)    # Find latest log
tail -f "$LOG"                                       # Follow real-time
grep "━━━ Phase:" "$LOG" | tail -1                   # Current phase
grep -c "FAILED\|ERROR" "$LOG"                       # Error count
testflinger status <UUID>                            # TF job phase
```

Logs go to `./logs/sunbeam-deploy-YYYYMMDD-HHMMSS.log`. Secrets are auto-redacted. A summary table with ✅/❌ per phase/step prints at the end of every run.

### Recovery

Re-run the failed phase: `--phase <name>`. Phases are idempotent. For cluster failures, check Sunbeam logs inside the VM:

```bash
ssh ubuntu@<IP>
lxc exec bm0 -- cat /home/ubuntu/snap/openstack/common/logs/sunbeam.log
lxc exec bm0 -- sudo -iu ubuntu sunbeam cluster list
```

### Post-Deployment Job Handling

- **Submitted jobs** (`--testflinger` or `--tf-job-file`): After success, the tool prompts `Cancel Testflinger job <UUID> and release the machine? [y/N]`. Default is No (keep alive).
- **Attached jobs** (`--tf-job-id`): No prompt — logs the job ID and how to cancel manually.
- **`--cancel-on-failure`**: Auto-cancels on failure without prompting.
- Manual cancel: `testflinger cancel <UUID>`. Jobs expire after `reserve_timeout` (default 3 days).
