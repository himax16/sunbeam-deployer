"""Phase 2 — VM deployment: install snap, prepare nodes, push manifest."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from sunbeam_deployer.config import DeployConfig
from sunbeam_deployer.executor import (
    push_file_to_vm,
    run_host,
    run_in_vm,
    wait_for_cloud_init,
    wait_for_vm,
    with_retry,
)
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

log = logging.getLogger("sunbeam_deployer.phases.vm_deploy")

PHASE = "vm-deploy"


def run_phase(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
) -> None:
    """Deploy the openstack snap and prepare all VMs for Sunbeam."""
    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        failed_nodes: list[str] = []

        with ThreadPoolExecutor(max_workers=cfg.concurrency.vm_deploy) as pool:
            futures = {
                pool.submit(_deploy_single_vm, cfg, mon, infra, node): node
                for node in infra.nodes
            }
            for future in as_completed(futures):
                node = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    log.error("VM %s deployment failed: %s", node.hostname, exc)
                    failed_nodes.append(node.hostname)

        if failed_nodes:
            raise RuntimeError(
                f"VM deployment failed for: {', '.join(failed_nodes)}"
            )

        mon.end_phase(PHASE, Status.SUCCESS)

    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


def _deploy_single_vm(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
    node: ComputeNode,
) -> None:
    """Deploy a single VM — called from the thread pool."""
    vm = node.name
    label = node.hostname

    _wait_ready(cfg, mon, vm, label)
    _install_snap(cfg, mon, vm, label)
    _setup_alias(cfg, mon, vm, label)

    if cfg.snap.source == "local":
        _connect_interfaces(cfg, mon, vm, label)

    _prepare_node(cfg, mon, vm, label)
    _push_manifest(cfg, mon, infra, vm, label)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _wait_ready(
    cfg: DeployConfig, mon: DeploymentMonitor, vm: str, label: str
) -> None:
    with mon.run_step(
        PHASE, f"{label}/wait-ready", f"Wait for {label} to be ready"
    ):
        if not wait_for_vm(vm, timeout=120):
            raise RuntimeError(f"VM {vm} not responsive")
        if not wait_for_cloud_init(vm, timeout=cfg.timeouts.cloud_init_wait):
            raise RuntimeError(f"cloud-init failed on {vm}")


def _install_snap(
    cfg: DeployConfig, mon: DeploymentMonitor, vm: str, label: str
) -> None:
    with mon.run_step(
        PHASE, f"{label}/install-snap", f"Install openstack snap on {label}"
    ):
        if cfg.snap.source == "store":
            _install_from_store(cfg, vm)
        else:
            _install_from_local(cfg, vm)


def _install_from_store(cfg: DeployConfig, vm: str) -> None:
    """Install the openstack snap from the snap store."""
    # Skip if already installed
    check = run_in_vm(vm, "snap list openstack", stream=False, timeout=30)
    if check.ok:
        log.info("openstack snap already installed on %s", vm)
        return

    cmd = f"sudo snap install openstack --channel={cfg.snap.channel}"
    if cfg.snap.revision:
        cmd += f" --revision={cfg.snap.revision}"

    result = with_retry(
        lambda: run_in_vm(vm, cmd, timeout=cfg.timeouts.snap_install),
        retries=3,
        delay=30,
        label=f"snap install on {vm}",
    )
    if not result.ok:
        raise RuntimeError(
            f"Failed to install openstack snap on {vm}: {result.stdout[-500:]}"
        )


def _install_from_local(cfg: DeployConfig, vm: str) -> None:
    """Install the openstack snap from a local .snap file."""
    local_path = os.path.expanduser(cfg.snap.local_path)
    remote_path = "/home/ubuntu/openstack.snap"

    # Push file to VM
    push_result = push_file_to_vm(vm, local_path, remote_path)
    if not push_result.ok:
        raise RuntimeError(
            f"Failed to push snap to {vm}: {push_result.stdout[-500:]}"
        )

    if cfg.snap.install_method == "try":
        # Unsquash and snap try
        run_in_vm(vm, f"unsquashfs {remote_path}", check=True, timeout=300)
        run_in_vm(vm, "sudo snap try squashfs-root/", check=True, timeout=120)
    else:
        # snap install --dangerous
        run_in_vm(
            vm,
            f"sudo snap install --dangerous {remote_path}",
            check=True,
            timeout=cfg.timeouts.snap_install,
        )


def _setup_alias(
    cfg: DeployConfig, mon: DeploymentMonitor, vm: str, label: str
) -> None:
    with mon.run_step(
        PHASE, f"{label}/alias", f"Set up sunbeam alias on {label}"
    ):
        run_in_vm(
            vm,
            "sudo snap alias openstack.sunbeam sunbeam",
            check=True,
            timeout=30,
        )


def _connect_interfaces(
    cfg: DeployConfig, mon: DeploymentMonitor, vm: str, label: str
) -> None:
    """Connect snap interfaces required when installing from local build."""
    with mon.run_step(
        PHASE, f"{label}/interfaces", f"Connect snap interfaces on {label}"
    ):
        interfaces = [
            "openstack:juju-bin juju:juju-bin",
            "openstack:dot-local-share-juju",
            "openstack:dot-config-openstack",
            "openstack:dot-local-share-openstack",
        ]
        for iface in interfaces:
            result = run_in_vm(
                vm, f"sudo snap connect {iface}", timeout=30, stream=False
            )
            if not result.ok:
                log.warning(
                    "Interface connect may have failed on %s: %s", vm, iface
                )


def _prepare_node(
    cfg: DeployConfig, mon: DeploymentMonitor, vm: str, label: str
) -> None:
    with mon.run_step(
        PHASE, f"{label}/prepare", f"Prepare node for Sunbeam on {label}"
    ):
        # Run prepare-node-script; skip newgrp (handled by fresh login shell)
        result = run_in_vm(
            vm,
            "sunbeam prepare-node-script --bootstrap | bash -x",
            timeout=cfg.timeouts.prepare_node,
        )
        if not result.ok:
            raise RuntimeError(
                f"prepare-node-script failed on {vm}: {result.stdout[-500:]}"
            )


def _push_manifest(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
    vm: str,
    label: str,
) -> None:
    if not cfg.sunbeam.manifest:
        return

    with mon.run_step(PHASE, f"{label}/manifest", f"Push manifest to {label}"):
        # Manifest lives on the deployment host (created by Terraform),
        # so check existence and push via run_host, not local filesystem.
        check = run_host(f"test -f {infra.manifest_path}", stream=False)
        if not check.ok:
            log.warning(
                "Manifest file not found at %s — skipping", infra.manifest_path
            )
            return

        result = run_host(
            f"lxc file push {infra.manifest_path}"
            f" {vm}/home/ubuntu/manifest.yaml",
            check=False,
            stream=True,
        )
        if not result.ok:
            raise RuntimeError(
                f"Failed to push manifest to {vm}: {result.stdout[-500:]}"
            )
