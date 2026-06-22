"""Phase 1 — Host setup: LXD, Terraform, bootstrap.

Delegates to a standalone bash script (``scripts/host-setup.sh``) that can
also be run independently on any Ubuntu machine.
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from sunbeam_deployer.config import DeployConfig
from sunbeam_deployer.executor import run_host
from sunbeam_deployer.monitor import DeploymentMonitor, Status

log = logging.getLogger("sunbeam_deployer.phases.host_setup")

PHASE = "host-setup"

# Path to the bundled standalone bash script
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "host-setup.sh"
)


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
        _run_host_setup_script(cfg, mon)
        info = _parse_terraform_outputs(cfg, mon)
        mon.end_phase(PHASE, Status.SUCCESS)
        return info
    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _run_host_setup_script(cfg: DeployConfig, mon: DeploymentMonitor) -> None:
    """Pipe the bundled host-setup.sh to the deployment host and run it."""
    with mon.run_step(
        PHASE,
        "host-setup-script",
        "Run host-setup script (LXD, Terraform, repo, bootstrap)",
    ):
        script = _SCRIPT_PATH.read_text()

        # Build environment variables for the script.
        # NOTE: REPO_DIR is intentionally NOT quoted — it must start with
        # ~ so the remote bash expands it to the remote $HOME.
        # All other values are safe to single-quote.
        env_vars = (
            f"REPO_URL={shlex.quote(cfg.repo_url)} "
            f"REPO_BRANCH={shlex.quote(cfg.repo_branch)} "
            f"REPO_DIR={cfg.repo_dir} "
            f"DEPLOY_MODE={shlex.quote(cfg.deploy_mode)} "
            f"TF_EXTRA_ARGS={shlex.quote(' '.join(cfg.terraform.extra_args))} "
            f"BOOTSTRAP_RETRIES={cfg.terraform.bootstrap_retries}"
        )

        # Pipe the script to bash on the remote host.
        # env vars are exported before bash reads the script from stdin.
        wrapped = f"export {env_vars} && bash -s"
        run_host(
            wrapped,
            input_text=script,
            check=True,
            timeout=cfg.timeouts.terraform_apply,
        )


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
