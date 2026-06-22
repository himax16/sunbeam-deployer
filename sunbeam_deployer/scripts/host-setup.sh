#!/usr/bin/env bash
#
# host-setup.sh — Standalone Sunbeam host setup (LXD, Terraform, repo, bootstrap)
#
# This script performs the host-setup phase of sunbeam-deployer independently.
# It can be called by the Python sunbeam-deployer tool OR run standalone on any
# Ubuntu machine that needs Sunbeam infra deployed.
#
# Usage (standalone):
#   wget -O - https://raw.githubusercontent.com/.../host-setup.sh | bash
#   bash host-setup.sh
#   REPO_URL=... bash host-setup.sh
#
# Usage (from sunbeam-deployer Python):
#   The Python tool exports env vars and runs this script via SSH.
#
# Environment variables (all optional — defaults shown):
#   REPO_URL          Git repo URL (default: https://github.com/himax16/sunbeam-proxified-dev.git)
#   REPO_BRANCH       Git branch to clone (default: main)
#   REPO_DIR          Clone destination (default: $HOME/sunbeam-proxified-dev)
#   DEPLOY_MODE       "manual" or "maas" (default: manual)
#   TF_EXTRA_ARGS     Extra args passed to bootstrap.sh (space-separated)
#   SKIP_BOOTSTRAP    Set to "true" to skip bootstrap.sh
#
# Options:
#   -h, --help        Show this help message
#
set -euo pipefail

# ==============================================================================
# Configuration — override via environment variables
# ==============================================================================
REPO_URL="${REPO_URL:-https://github.com/himax16/sunbeam-proxified-dev.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="${REPO_DIR:-$HOME/sunbeam-proxified-dev}"
DEPLOY_MODE="${DEPLOY_MODE:-manual}"
TF_EXTRA_ARGS="${TF_EXTRA_ARGS:-}"
SKIP_BOOTSTRAP="${SKIP_BOOTSTRAP:-false}"
BOOTSTRAP_RETRIES="${BOOTSTRAP_RETRIES:-1}"
VM_BOOT_TIMEOUT="${VM_BOOT_TIMEOUT:-5m}"

# ==============================================================================
# Helpers
# ==============================================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()   { echo -e "${YELLOW}[!]${NC} $1"; }
err()    { echo -e "${RED}[✗]${NC} $1" >&2; }
info()   { echo -e "${CYAN}[→]${NC} $1"; }
header() { echo -e "\n${CYAN}═══════════════════════════════════════${NC}"; echo -e "${CYAN}  $1${NC}"; echo -e "${CYAN}═══════════════════════════════════════${NC}"; }

trap 'err "Script failed at line $LINENO."; exit 1' ERR

# ==============================================================================
# Argument parsing
# ==============================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '/^#/{/^#!/d; /^# =/q; p}' "$0" | sed 's/^# //; s/^#//'
            exit 0 ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

# ==============================================================================
# Pre-flight
# ==============================================================================
header "Pre-flight checks"
for cmd in sudo snap git; do
    if ! command -v "$cmd" &>/dev/null; then
        err "'$cmd' is required but not installed."
        exit 1
    fi
done

if [ "$(id -u)" -eq 0 ]; then
    warn "Running as root. Some commands (lxd init) may behave differently."
fi
log "All prerequisites met."

# ==============================================================================
# Step 1 — Install LXD
# ==============================================================================
header "Step 1/5: Install LXD"
if command -v lxd &>/dev/null; then
    log "LXD already installed."
else
    info "Installing LXD via snap..."
    sudo snap install lxd
    # Wait for the LXD socket to appear
    for i in $(seq 1 30); do
        if [ -S /var/snap/lxd/common/lxd/unix.socket ]; then break; fi
        sleep 1
    done
    # Give lxd daemon a moment to settle
    sleep 2
    log "LXD installed."
fi

# ==============================================================================
# Step 2 — Initialize LXD
# ==============================================================================
header "Step 2/5: Initialize LXD"
if lxc storage show default &>/dev/null; then
    log "LXD already initialized."
else
    info "Initializing LXD with defaults..."
    lxd init --auto
    log "LXD initialized."
fi

# ==============================================================================
# Step 3 — Install Terraform
# ==============================================================================
header "Step 3/5: Install Terraform"
if command -v terraform &>/dev/null; then
    log "Terraform already installed."
else
    info "Installing Terraform via snap..."
    sudo snap install terraform --classic
    log "Terraform installed."
fi

# ==============================================================================
# Step 4 — Clone repository
# ==============================================================================
header "Step 4/5: Clone deployment repository"
if [ -d "$REPO_DIR/.git" ]; then
    info "Repository exists at $REPO_DIR. Pulling latest..."
    git -C "$REPO_DIR" pull --ff-only
    log "Repository updated."
else
    # Remove stale directory if it exists without .git
    rm -rf "$REPO_DIR"
    info "Cloning $REPO_URL → $REPO_DIR (branch: $REPO_BRANCH)..."
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
    log "Repository cloned."
fi

# ==============================================================================
# Step 5 — Bootstrap the plan
# ==============================================================================
header "Step 5/5: Bootstrap the plan"
if [ "$SKIP_BOOTSTRAP" = true ]; then
    warn "Skipping bootstrap (SKIP_BOOTSTRAP=true)."
else
    BOOTSTRAP_SCRIPT="$REPO_DIR/bootstrap.sh"
    if [ -f "$BOOTSTRAP_SCRIPT" ]; then
        chmod +x "$BOOTSTRAP_SCRIPT"

        # Build bootstrap command line. VM_BOOT_TIMEOUT is passed through
        # to the Terraform module via -var to control LXD/MAAS VM start timeout.
        BOOTSTRAP_CMD="$BOOTSTRAP_SCRIPT"
        if [ "$DEPLOY_MODE" = "maas" ]; then
            BOOTSTRAP_CMD="$BOOTSTRAP_CMD --maas"
        fi
        for arg in $TF_EXTRA_ARGS; do
            BOOTSTRAP_CMD="$BOOTSTRAP_CMD $arg"
        done
        # Append vm_boot_timeout as a terraform variable override
        BOOTSTRAP_CMD="$BOOTSTRAP_CMD -var vm_boot_timeout=$VM_BOOT_TIMEOUT"

        info "Executing: $BOOTSTRAP_CMD"

        # Terraform's LXD provider has a default 10-minute VM boot timeout,
        # but module timeouts can be set via the vm_boot_timeout variable.
        # For legacy repos without the variable, --tf-arg can be used.
        # Retry up to BOOTSTRAP_RETRIES times: terraform apply is idempotent —
        # it picks up where it left off and VMs that already started are
        # re-detected.
        BOOTSTRAP_RC=0
        ATTEMPT=0
        MAX_ATTEMPTS=$((BOOTSTRAP_RETRIES + 1))

        while [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; do
            ATTEMPT=$((ATTEMPT + 1))
            BOOTSTRAP_RC=0
            eval "$BOOTSTRAP_CMD" || BOOTSTRAP_RC=$?

            if [ "$BOOTSTRAP_RC" -eq 0 ]; then
                break
            fi

            if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then
                warn "Bootstrap exited with code $BOOTSTRAP_RC — retrying (attempt $ATTEMPT/$MAX_ATTEMPTS)…"
                # Un-taint any lxd_instance resources that Terraform marked as
                # tainted on failure so they aren't destroyed and recreated.
                # VMs that are still "Running (initializing)" will finish
                # starting and be re-detected on the next apply.
                plan_dir="$REPO_DIR/manual-infra"
                if [ "$DEPLOY_MODE" = "maas" ]; then
                    plan_dir="$REPO_DIR/maas-infra"
                fi
                if [ -d "$plan_dir/.terraform" ]; then
                    for tainted in $(cd "$plan_dir" && terraform state list 2>/dev/null | grep 'lxd_instance.compute\|maas_vm_host_machine.compute'); do
                        (cd "$plan_dir" && terraform untaint "$tainted" 2>/dev/null) || true
                    done
                fi
                set +e
            else
                err "Bootstrap failed after $MAX_ATTEMPTS attempts (exit code $BOOTSTRAP_RC)"
                exit 1
            fi
        done

        set -e

        log "Bootstrap completed successfully."
    else
        warn "bootstrap.sh not found at $BOOTSTRAP_SCRIPT. Skipping."
    fi
fi

# ==============================================================================
# Done
# ==============================================================================
header "✅ Host setup completed successfully"
