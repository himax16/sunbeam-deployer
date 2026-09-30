"""Phase 4 — Embedded COS observability: configure, enable, verify."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

import yaml

from sunbeam_deployer.config import DeployConfig, _deep_merge
from sunbeam_deployer.executor import (
    push_file_to_vm,
    run_host,
    run_in_vm,
)
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

log = logging.getLogger("sunbeam_deployer.phases.observability")

PHASE = "observability"

_COS_MODEL = "observability"
_COS_APPS = (
    "traefik",
    "alertmanager",
    "prometheus",
    "grafana",
    "catalogue",
    "loki",
)
_GRAFANA_PATH = "/observability-grafana"
_VERIFY_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "verify-observability.py"
)
_VERIFY_VM_PATH = "/tmp/verify_observability.py"


def run_phase(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
) -> None:
    """Enable embedded COS observability on the primary node."""
    if not cfg.observability.enabled:
        log.info("Observability disabled — skipping")
        return

    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        if not infra.nodes:
            raise RuntimeError(
                "No compute nodes found — cannot enable observability"
            )

        primary = infra.nodes[0]

        _configure_manifest(cfg, mon, infra, primary)
        _enable_observability(cfg, mon, primary)
        _wait_cos_active(cfg, mon, primary)
        _report_dashboard_url(cfg, mon, primary)
        _verify_features(cfg, mon, primary)

        mon.end_phase(PHASE, Status.SUCCESS)

    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _observability_manifest_block(cfg: DeployConfig) -> dict:
    """Return the nested manifest block for the configured COS charms.

    ``channel`` is set explicitly (not just ``storage``) because Sunbeam's
    manifest merge replaces a charm entry wholesale; omitting it would clobber
    the snap's default and silently change the track. The snap's bundled
    ``deploy-cos`` terraform hardcodes ``ubuntu@20.04``, so the channel must be
    one that still ships a compatible base (see the base-fix patch).
    """
    s = cfg.observability.storage
    c = cfg.observability.channel
    return {
        "features": {
            "observability": {
                "embedded": {
                    "software": {
                        "charms": {
                            "prometheus-k8s": {
                                "channel": c,
                                "storage": {"database": s.prometheus},
                            },
                            "loki-k8s": {
                                "channel": c,
                                "storage": {
                                    "active-index-directory": s.loki_index,
                                    "loki-chunks": s.loki_chunks,
                                },
                            },
                            "grafana-k8s": {
                                "channel": c,
                                "storage": {"database": s.grafana},
                            },
                            "alertmanager-k8s": {
                                "channel": c,
                                "storage": {"data": s.alertmanager},
                            },
                        }
                    }
                }
            }
        }
    }


def _configure_manifest(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
    primary: ComputeNode,
) -> None:
    """Build a complete manifest (original + observability) and push it."""
    with mon.run_step(
        PHASE, "configure-manifest", "Configure observability manifest"
    ):
        if cfg.sunbeam.manifest:
            result = run_host(f"cat {infra.manifest_path}", stream=False)
            if not result.ok:
                raise RuntimeError(
                    f"Original manifest not found at {infra.manifest_path} "
                    "— required to enable observability safely"
                )
            manifest = yaml.safe_load(result.stdout)
            if (
                not isinstance(manifest, dict)
                or not manifest
                or "core" not in manifest
            ):
                raise RuntimeError(
                    f"Manifest at {infra.manifest_path} looks incomplete; "
                    "'sunbeam enable --manifest' replaces the "
                    "cluster-stored manifest, so a complete original "
                    "manifest is required"
                )
        else:
            # Bootstrapped without a custom manifest — nothing to preserve.
            manifest = {}

        existing = (
            (manifest.get("features") or {}).get("observability")
            if isinstance(manifest, dict)
            else None
        )

        if existing is not None and not _prompt_override_observability():
            log.info("Keeping existing observability segment in manifest")
            return

        merged = _deep_merge(manifest, _observability_manifest_block(cfg))
        dumped = yaml.safe_dump(merged)
        b64 = base64.b64encode(dumped.encode()).decode()
        write = run_host(
            f"echo {b64} | base64 -d > {infra.manifest_path}",
            stream=False,
        )
        if not write.ok:
            raise RuntimeError(
                f"Failed to write observability manifest to "
                f"{infra.manifest_path}"
            )

        push = run_host(
            f"lxc file push {infra.manifest_path} "
            f"{primary.name}/home/ubuntu/manifest.yaml",
            stream=False,
        )
        if not push.ok:
            raise RuntimeError(
                f"Failed to push observability manifest to {primary.name}"
            )


def _prompt_override_observability() -> bool:
    """Ask whether to override an existing observability segment."""
    try:
        answer = input(
            "Original manifest already contains an observability segment. "
            "Override with configured observability storage? [y/N] "
        )
    except EOFError:
        log.info("Non-interactive session — keeping existing manifest segment")
        return False
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _enable_observability(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Run ``sunbeam enable observability embedded`` on the primary node."""
    with mon.run_step(
        PHASE, "enable-observability", "Enable embedded COS observability"
    ):
        cmd = "sunbeam enable --manifest ~/manifest.yaml observability embedded"
        log.info("Enabling observability on %s", primary.fqdn)
        result = run_in_vm(
            primary.name,
            cmd,
            timeout=cfg.timeouts.observability_enable,
        )
        if not result.ok:
            raise RuntimeError(
                f"Failed to enable observability: {result.stdout[-500:]}"
            )
        log.info("Observability enabled on %s", primary.hostname)


def _wait_cos_active(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Poll juju until all COS applications report active status."""
    with mon.run_step(
        PHASE, "wait-cos-active", "Wait for COS applications to be active"
    ):
        deadline = time.monotonic() + cfg.timeouts.observability_verify
        while True:
            result = run_in_vm(
                primary.name,
                "juju status -m observability --format json",
                stream=False,
                timeout=120,
            )

            non_active: list[str] = []
            if result.ok:
                try:
                    applications = json.loads(result.stdout)["applications"]
                except (ValueError, KeyError):
                    applications = {}
                for app in _COS_APPS:
                    status = (
                        applications.get(app, {})
                        .get("application-status", {})
                        .get("current")
                    )
                    if status != "active":
                        non_active.append(f"{app}({status})")
                if not non_active:
                    log.info("All COS applications are active")
                    return
            else:
                non_active = list(_COS_APPS)

            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for COS applications to become "
                    "active: " + ", ".join(non_active)
                )
            time.sleep(15)


def _report_dashboard_url(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Extract and log the Grafana dashboard URL."""
    with mon.run_step(PHASE, "dashboard-url", "Get Grafana dashboard URL"):
        result = run_in_vm(
            primary.name,
            "sunbeam observability dashboard-url",
            stream=False,
            timeout=120,
        )
        if not result.ok:
            raise RuntimeError(
                f"Failed to get dashboard URL: {result.stdout[-500:]}"
            )
        match = re.search(r"https?://\S+", result.stdout)
        if match is None or _GRAFANA_PATH not in match.group(0):
            raise RuntimeError(
                "Could not determine Grafana dashboard URL from: "
                f"{result.stdout[-500:]}"
            )
        url = match.group(0)
        log.info("Grafana dashboard URL: %s", url)
        log.info(
            "Retrieve the Grafana admin password with: "
            "juju run -m observability grafana/leader get-admin-password"
        )


def _verify_features(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Push and run the feature verification script inside the primary VM."""
    with mon.run_step(
        PHASE, "verify-features", "Verify observability features"
    ):
        # Leave headroom under the step timeout for the script's own setup.
        script_timeout = max(60, cfg.timeouts.observability_verify - 120)
        push = push_file_to_vm(
            primary.name, str(_VERIFY_SCRIPT), _VERIFY_VM_PATH
        )
        if not push.ok:
            raise RuntimeError(
                f"Failed to push verification script to {primary.name}"
            )

        result = run_in_vm(
            primary.name,
            f"python3 {_VERIFY_VM_PATH} --timeout {script_timeout}",
            timeout=cfg.timeouts.observability_verify,
        )
        for line in result.stdout.splitlines():
            if line.startswith(("PASS", "FAIL")):
                log.info("%s", line)
        if not result.ok:
            raise RuntimeError(
                "Observability feature verification failed: "
                f"{result.stdout[-1500:]}"
            )
