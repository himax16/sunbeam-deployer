"""Phase 1 — Host setup: LXD, Terraform, bootstrap."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sunbeam_deployer.config import DeployConfig
from sunbeam_deployer.executor import run_host
from sunbeam_deployer.monitor import DeploymentMonitor, Status

log = logging.getLogger("sunbeam_deployer.phases.host_setup")

PHASE = "host-setup"


@dataclass
class ComputeNode:
    """Represents one LXD VM produced by Terraform."""

    name: str
    fqdn: str
    hostname: str
    ip: str
    roles: list[str]
    osd_devices: list[str] = field(default_factory=list)


@dataclass
class InfraInfo:
    """Everything the later phases need from the host-setup phase."""

    nodes: list[ComputeNode]
    plan_dir: str
    manifest_path: str
    ssh_private_key_path: str
    management_domain: str = ""


def run_phase(cfg: DeployConfig, mon: DeploymentMonitor) -> InfraInfo:
    """Execute the host-setup phase and return infrastructure metadata."""
    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        _install_lxd(cfg, mon)
        _install_terraform(cfg, mon)
        _clone_repo(cfg, mon)
        _run_bootstrap(cfg, mon)
        info = _parse_terraform_outputs(cfg, mon)
        mon.end_phase(PHASE, Status.SUCCESS)
        return info
    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _install_lxd(cfg: DeployConfig, mon: DeploymentMonitor) -> None:
    with mon.run_step(PHASE, "install-lxd", "Install and initialise LXD"):
        result = run_host("which lxc", stream=False)
        if not result.ok:
            run_host(
                "sudo snap install lxd",
                check=True,
                timeout=cfg.timeouts.snap_install,
            )
        else:
            log.info("LXD already installed")

        # Ensure LXD is initialised — bootstrap.sh's dir-driver preseed
        # fails on block devices, so pre-init with --auto to create the
        # default storage pool.  bootstrap.sh detects this and skips init.
        init_check = run_host("lxc storage show default", stream=False)
        if init_check.ok:
            log.info("LXD already initialised")
        else:
            log.info("Initialising LXD with --auto")
            run_host("lxd init --auto", check=True, timeout=60)


def _install_terraform(cfg: DeployConfig, mon: DeploymentMonitor) -> None:
    with mon.run_step(PHASE, "install-terraform", "Install Terraform snap"):
        result = run_host("which terraform", stream=False)
        if result.ok:
            log.info("Terraform already installed")
            return
        run_host(
            "sudo snap install terraform --classic",
            check=True,
            timeout=cfg.timeouts.snap_install,
        )


def _clone_repo(cfg: DeployConfig, mon: DeploymentMonitor) -> None:
    with mon.run_step(PHASE, "clone-repo", "Clone infrastructure repository"):
        repo_dir = cfg.repo_dir

        # Check if repo is already cloned on the target (local or remote)
        result = run_host(f"test -d {repo_dir}/.git", stream=False)
        if result.ok:
            log.info("Repo already cloned at %s — pulling latest", repo_dir)
            run_host(f"git -C {repo_dir} pull --ff-only", stream=True)
            return

        # Remove stale directory if it exists without .git
        run_host(f"rm -rf {repo_dir}", stream=False)

        run_host(
            f"git clone --branch {cfg.repo_branch} {cfg.repo_url} {repo_dir}",
            check=True,
        )


def _run_bootstrap(cfg: DeployConfig, mon: DeploymentMonitor) -> None:
    with mon.run_step(
        PHASE, "bootstrap", "Run infrastructure bootstrap (Terraform)"
    ):
        repo_dir = cfg.repo_dir
        bootstrap_script = f"{repo_dir}/bootstrap.sh"

        run_host(f"chmod +x {bootstrap_script}", check=True)

        cmd = bootstrap_script
        if cfg.deploy_mode == "maas":
            cmd += " --maas"
        for arg in cfg.terraform.extra_args:
            cmd += f" {arg}"

        run_host(cmd, check=True, timeout=cfg.timeouts.terraform_apply)


def _parse_terraform_outputs(
    cfg: DeployConfig, mon: DeploymentMonitor
) -> InfraInfo:
    with mon.run_step(PHASE, "parse-outputs", "Parse Terraform outputs"):
        repo_dir = cfg.repo_dir
        plan_dir = f"{repo_dir}/manual-infra"

        # Get compute_nodes from Terraform
        # Use `cd` + `terraform output` instead of `-chdir=` because
        # bash doesn't expand ~ inside flag values like -chdir=~/...
        result = run_host(
            f"cd {plan_dir} && terraform output -json compute_nodes",
            check=True,
            stream=False,
        )
        nodes_json = json.loads(result.stdout)

        nodes: list[ComputeNode] = []
        for node_data in nodes_json:
            name = node_data["name"]
            # Priority: config node_roles > global default > Terraform roles
            if name in cfg.sunbeam.node_roles:
                roles = cfg.sunbeam.node_roles[name]
            elif cfg.sunbeam.roles:
                roles = cfg.sunbeam.roles
            elif node_data.get("roles"):
                roles = node_data["roles"]
            else:
                roles = ["control", "compute"]

            nodes.append(
                ComputeNode(
                    name=name,
                    fqdn=node_data["fqdn"],
                    hostname=node_data["hostname"],
                    ip=node_data["ip"],
                    roles=roles,
                    osd_devices=node_data.get("osd_devices", []),
                )
            )

        # Get network topology for domain info
        topo_result = run_host(
            f"cd {plan_dir} && terraform output -json network_topology",
            stream=False,
        )
        domain = ""
        if topo_result.ok:
            topo = json.loads(topo_result.stdout)
            domain = topo.get("management", {}).get("domain", "")

        manifest_path = f"{plan_dir}/manifest.yaml"
        ssh_key_path = f"{plan_dir}/ssh_private_key"

        log.info(
            "Discovered %d nodes: %s",
            len(nodes),
            ", ".join(f"{n.hostname}({','.join(n.roles)})" for n in nodes),
        )

        return InfraInfo(
            nodes=nodes,
            plan_dir=plan_dir,
            manifest_path=manifest_path,
            ssh_private_key_path=ssh_key_path,
            management_domain=domain,
        )
