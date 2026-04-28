"""Phase 3 — Cluster lifecycle: bootstrap first node, join remaining nodes."""

from __future__ import annotations

import logging
import re

from sunbeam_deployer.config import DeployConfig
from sunbeam_deployer.executor import run_in_vm
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

log = logging.getLogger("sunbeam_deployer.phases.cluster")

PHASE = "cluster"


def run_phase(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
) -> None:
    """Bootstrap the first node, then join the remaining nodes."""
    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        if not infra.nodes:
            raise RuntimeError(
                "No compute nodes found — cannot bootstrap cluster"
            )

        cluster_nodes = _resolve_cluster_nodes(cfg, infra)

        primary = cluster_nodes[0]
        secondaries = cluster_nodes[1:]

        log.info(
            "Cluster topology: primary=%s, secondaries=[%s]",
            primary.hostname,
            ", ".join(n.hostname for n in secondaries),
        )

        # Validate DNS resolution across cluster nodes
        if len(cluster_nodes) > 1:
            _validate_dns(cfg, mon, cluster_nodes)
        else:
            log.info("Single-node cluster — skipping DNS validation")

        # Bootstrap the primary node
        _bootstrap(cfg, mon, primary)

        # Join each secondary node sequentially
        for node in secondaries:
            _join_node(cfg, mon, primary, node)

        mon.end_phase(PHASE, Status.SUCCESS)

    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


def _resolve_cluster_nodes(
    cfg: DeployConfig,
    infra: InfraInfo,
) -> list[ComputeNode]:
    """Return the nodes that participate in the cluster.

    When ``cfg.sunbeam.cluster_node_count`` is set to a positive
    integer, only the first *N* nodes from Terraform output
    participate.  When 0 (the default), all nodes participate.
    """
    count = cfg.sunbeam.cluster_node_count

    if count == 0 or count >= len(infra.nodes):
        return infra.nodes

    cluster_nodes = infra.nodes[:count]
    skipped = infra.nodes[count:]

    log.info(
        "Nodes excluded from cluster: %s",
        ", ".join(n.hostname for n in skipped),
    )

    return cluster_nodes


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _validate_dns(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    nodes: list[ComputeNode],
) -> None:
    """Check that each cluster node can resolve every other's FQDN."""
    with mon.run_step(
        PHASE, "dns-validation", "Validate cross-node DNS resolution"
    ):
        all_ok = True
        for src in nodes:
            for dst in nodes:
                if src.name == dst.name:
                    continue
                result = run_in_vm(
                    src.name,
                    f"getent hosts {dst.fqdn}",
                    timeout=15,
                    stream=False,
                )
                if not result.ok:
                    log.error(
                        "DNS failed: %s cannot resolve %s",
                        src.hostname,
                        dst.fqdn,
                    )
                    all_ok = False
                else:
                    log.debug(
                        "DNS OK: %s -> %s (%s)",
                        src.hostname,
                        dst.fqdn,
                        result.stdout.strip().split()[0],
                    )

        if not all_ok:
            raise RuntimeError(
                "DNS validation failed — nodes cannot "
                "resolve each other's FQDNs. "
                "Check the DNS/dnsmasq configuration."
            )
        log.info("All nodes can resolve each other's FQDNs")


def _bootstrap(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    node: ComputeNode,
) -> None:
    """Bootstrap the Sunbeam cluster on the primary node."""
    step_name = f"bootstrap-{node.hostname}"
    desc = f"Bootstrap Sunbeam cluster on {node.hostname} ({node.fqdn})"

    with mon.run_step(PHASE, step_name, desc):
        roles = ",".join(node.roles)
        cmd = f"sunbeam -v cluster bootstrap --role {roles}"

        if cfg.sunbeam.manifest and cfg.sunbeam.accept_defaults:
            cmd += " --manifest ~/manifest.yaml --accept-defaults"
        elif cfg.sunbeam.manifest:
            cmd += " --manifest ~/manifest.yaml"
        elif cfg.sunbeam.accept_defaults:
            cmd += " --accept-defaults"

        for extra in cfg.sunbeam.bootstrap_extra_args:
            cmd += f" {extra}"

        log.info("Bootstrapping cluster with roles=%s on %s", roles, node.fqdn)
        result = run_in_vm(
            node.name,
            cmd,
            timeout=cfg.timeouts.cluster_bootstrap,
        )

        if not result.ok:
            raise RuntimeError(
                f"Cluster bootstrap failed on "
                f"{node.hostname}:\n"
                f"{result.stdout[-2000:]}"
            )

        log.info("Cluster bootstrap completed on %s", node.hostname)


def _join_node(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
    node: ComputeNode,
) -> None:
    """Generate a join token on the primary and join a secondary node."""
    step_name = f"join-{node.hostname}"
    desc = f"Join {node.hostname} ({node.fqdn}) to cluster"

    with mon.run_step(PHASE, step_name, desc):
        # Step 1: Generate join token on the primary node
        log.info(
            "Generating join token for %s on %s", node.fqdn, primary.hostname
        )
        token_result = run_in_vm(
            primary.name,
            f"sunbeam cluster add --format yaml {node.fqdn}",
            timeout=120,
        )

        if not token_result.ok:
            raise RuntimeError(
                f"Failed to generate join token for "
                f"{node.fqdn}:\n"
                f"{token_result.stdout[-1000:]}"
            )

        token = _extract_token(token_result.stdout)
        if not token:
            raise RuntimeError(
                "Could not extract join token from "
                f"output:\n{token_result.stdout[-1000:]}"
            )

        log.debug("Join token obtained for %s", node.fqdn)

        # Step 2: Join the node to the cluster
        roles = ",".join(node.roles)
        join_cmd = f"sunbeam -v cluster join --role {roles} {token}"

        log.info("Joining %s with roles=%s", node.fqdn, roles)
        join_result = run_in_vm(
            node.name,
            join_cmd,
            timeout=cfg.timeouts.cluster_join,
        )

        if not join_result.ok:
            raise RuntimeError(
                f"Cluster join failed on "
                f"{node.hostname}:\n"
                f"{join_result.stdout[-2000:]}"
            )

        log.info("Node %s joined the cluster", node.hostname)


def _extract_token(output: str) -> str | None:
    """Extract the join token from ``sunbeam cluster add`` output.

    The output may be a YAML document with a ``token:`` field, or the token
    may appear as a bare base64 string on its own line.
    """
    # Try YAML parsing first
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("token:"):
            value = stripped[len("token:") :].strip().strip("'\"")
            if value:
                return value

    # Fallback: look for a long base64-like token on its own line
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if re.match(r"^[A-Za-z0-9+/=_.-]{20,}$", stripped):
            return stripped

    return None
