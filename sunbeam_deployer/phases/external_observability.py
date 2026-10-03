"""Phase 5 — External COS observability.

Deploy a standalone COS Lite stack and attach it via
``sunbeam enable observability external``. ``observability.external.controller``
selects the controller: ``None`` uses the cluster's own controller; a name
bootstraps a dedicated controller.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from sunbeam_deployer.config import DeployConfig, ExternalObservabilityConfig
from sunbeam_deployer.executor import (
    CommandResult,
    push_file_to_vm,
    run_in_vm,
)
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

log = logging.getLogger("sunbeam_deployer.phases.external_observability")

PHASE = "external-observability"

# COS Lite applications. Mirrors the embedded COS stack (_COS_APPS).
_COS_APPS = (
    "traefik",
    "alertmanager",
    "prometheus",
    "grafana",
    "catalogue",
    "loki",
)

# Offer endpoints consumed by `sunbeam enable observability external`:
#   grafana:grafana-dashboard       -> grafana-dashboards-provider relation
#   prometheus:receive-remote-write -> send-remote-write relation
#   loki:logging                    -> send-loki-logs relation
_OFFERS = (
    ("grafana", "grafana-dashboard"),
    ("prometheus", "receive-remote-write"),
    ("loki", "logging"),
)

_VERIFY_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "verify-observability.py"
)
_VERIFY_VM_PATH = "/tmp/verify_observability.py"


def _is_separate(ext: ExternalObservabilityConfig) -> bool:
    """Return True when COS is deployed on its own dedicated controller."""
    return bool(ext.controller)


def _controller_name(ext: ExternalObservabilityConfig) -> str:
    """Return the controller name passed to ``sunbeam enable``.

    Falls back to the cluster's controller when no dedicated COS controller is
    configured.
    """
    return ext.controller or "sunbeam-controller"


def run_phase(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    infra: InfraInfo,
) -> None:
    """Deploy, attach, and verify an external COS stack."""
    ext = cfg.observability.external
    if not ext.enabled:
        log.info("External observability disabled — skipping")
        return

    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        if not infra.nodes:
            raise RuntimeError(
                "No compute nodes found — cannot attach external observability"
            )

        primary = infra.nodes[0]

        if _is_separate(ext):
            _bootstrap_controller(cfg, mon, primary)
        _deploy_cos_stack(cfg, mon, primary)
        offer_urls = _create_offers(cfg, mon, primary)
        _register_controller(cfg, mon, primary)
        _enable_external_observability(cfg, mon, primary, offer_urls)
        _integrate_collectors(cfg, mon, primary, offer_urls)
        _report_dashboard_url(cfg, mon, primary)
        _verify_features(cfg, mon, primary)

        mon.end_phase(PHASE, Status.SUCCESS)

    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _bootstrap_controller(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Bootstrap the dedicated COS controller (idempotent)."""
    ext = cfg.observability.external
    with mon.run_step(
        PHASE,
        "bootstrap-controller",
        f"Bootstrap controller {ext.controller}",
    ):
        if _controller_exists(primary, ext.controller):
            log.info("Controller %s already exists", ext.controller)
            return
        cloud = _k8s_cloud(primary)
        result = run_in_vm(
            primary.name,
            f"juju bootstrap {cloud} {ext.controller}",
            timeout=900,
        )
        if not result.ok:
            raise RuntimeError(
                f"Failed to bootstrap controller {ext.controller}: "
                f"{result.stdout[-500:]}"
            )


def _deploy_cos_stack(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Deploy the cos-lite bundle, on a dedicated or the cluster controller."""
    ext = cfg.observability.external
    separate = _is_separate(ext)
    with mon.run_step(
        PHASE, "deploy-cos", f"Deploy COS Lite in model {ext.model}"
    ):
        if separate:
            run_in_vm(
                primary.name, f"juju switch {ext.controller}", stream=False
            )
        if not _model_exists(primary, ext.model):
            if separate:
                add_cmd = f"juju add-model {ext.model}"
            else:
                add_cmd = f"juju add-model {ext.model} {_k8s_cloud(primary)}"
            add = run_in_vm(primary.name, add_cmd, stream=False)
            if not add.ok:
                raise RuntimeError(
                    f"Failed to add model {ext.model}: {add.stdout[-500:]}"
                )

        deploy = run_in_vm(
            primary.name,
            f"juju deploy -m {ext.model} cos-lite --channel {ext.channel} "
            "--trust",
            timeout=cfg.timeouts.observability_enable,
        )
        if not deploy.ok:
            raise RuntimeError(
                f"Failed to deploy cos-lite in {ext.model}: "
                f"{deploy.stdout[-500:]}"
            )

    _wait_cos_active(cfg, mon, primary)


def _wait_cos_active(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Poll juju until every COS application in the model is active."""
    ext = cfg.observability.external
    with mon.run_step(
        PHASE, "wait-cos-active", f"Wait for COS apps active in {ext.model}"
    ):
        deadline = time.monotonic() + cfg.timeouts.observability_verify
        while True:
            result = run_in_vm(
                primary.name,
                f"juju status -m {ext.model} --format json",
                stream=False,
                timeout=120,
            )

            non_active: list[str] = []
            if result.ok:
                applications = _parse_json(result.stdout).get(
                    "applications", {}
                )
                for app in _COS_APPS:
                    status = (
                        applications.get(app, {})
                        .get("application-status", {})
                        .get("current")
                    )
                    if status != "active":
                        non_active.append(f"{app}({status})")
                if not non_active:
                    log.info("All COS applications are active in %s", ext.model)
                    return
            else:
                non_active = list(_COS_APPS)

            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for COS apps in {ext.model}: "
                    + ", ".join(non_active)
                )
            time.sleep(15)


def _create_offers(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> tuple[str, str, str]:
    """Offer endpoints; return the (grafana, prometheus, loki) offer URLs."""
    ext = cfg.observability.external
    with mon.run_step(
        PHASE, "offer-endpoints", f"Offer COS endpoints in {ext.model}"
    ):
        if _is_separate(ext):
            run_in_vm(
                primary.name, f"juju switch {ext.controller}", stream=False
            )
        urls: list[str] = []
        for app, endpoint in _OFFERS:
            offer = run_in_vm(
                primary.name,
                f"juju offer {ext.model}.{app}:{endpoint}",
                stream=False,
                timeout=120,
            )
            url = _parse_offer_url(offer.stdout)
            if offer.ok and url:
                urls.append(url)
                continue
            # Existing offer returns non-zero; fall back to its default name.
            if not offer.ok and "already exists" in offer.stdout.lower():
                owner = _model_owner(primary, ext.model)
                urls.append(f"{owner}/{ext.model}.{app}")
                continue
            raise RuntimeError(
                f"Failed to offer {app}:{endpoint}: {offer.stdout[-300:]}"
            )
    return (urls[0], urls[1], urls[2])


def _register_controller(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Create the ``<controller>.yaml`` account file the pre-flight check
    expects (a copy of the cluster's account)."""
    ext = cfg.observability.external
    controller = _controller_name(ext)
    account = f"~/snap/openstack/current/{controller}.yaml"
    with mon.run_step(
        PHASE,
        "register-controller",
        f"Register controller {controller}",
    ):
        exists = run_in_vm(
            primary.name, f"test -f {account}", stream=False, timeout=60
        )
        if exists.ok:
            return
        copy = run_in_vm(
            primary.name,
            f"cp ~/snap/openstack/current/account.yaml {account}",
            stream=False,
            timeout=60,
        )
        if not copy.ok:
            raise RuntimeError(
                f"Failed to register controller {controller}: "
                f"{copy.stdout[-300:]}"
            )


def _enable_external_observability(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
    offer_urls: tuple[str, str, str],
) -> None:
    """Run ``sunbeam enable observability external`` on the primary node."""
    ext = cfg.observability.external
    grafana, prometheus, loki = offer_urls
    with mon.run_step(
        PHASE, "enable-external", "Attach external COS offers to the cluster"
    ):
        cmd = (
            f"sunbeam -v enable observability external "
            f"{_controller_name(ext)} {grafana} {prometheus} {loki}"
        )
        log.info(
            "Attaching external observability controller=%s",
            _controller_name(ext),
        )
        result = run_in_vm(
            primary.name,
            cmd,
            timeout=cfg.timeouts.observability_enable,
        )
        if not result.ok:
            # TEMPORARY WORKAROUND: the snap's enable fails on the machine
            # model; _integrate_collectors completes it below.
            log.warning(
                "sunbeam enable returned non-zero; completing the "
                "collector integrations manually"
            )
        log.info("External observability enabled on %s", primary.hostname)


def _integrate_collectors(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
    offer_urls: tuple[str, str, str],
) -> None:
    """Integrate the collectors the snap's enable step misses.

    TEMPORARY WORKAROUND — remove once the snap's enable is fixed (see
    docs/external-observability-findings.md). Back-fills the infra collector
    (pre-PR-#940 snaps) and the machine collector (``admin/openstack-machines``
    short-name bug). Re-integrating the main collector is idempotent.
    """
    ext = cfg.observability.external
    endpoints = (
        "grafana-dashboards-provider",
        "send-remote-write",
        "send-loki-logs",
    )
    with mon.run_step(
        PHASE,
        "integrate-collectors",
        "Integrate collectors with COS offers",
    ):
        if _is_separate(ext):
            run_in_vm(
                primary.name, "juju switch sunbeam-controller", stream=False
            )
        targets = (
            ("openstack", "opentelemetry-collector"),
            ("openstack", "opentelemetry-collector-infra"),
            (
                _full_model_name(primary, "openstack-machines"),
                "opentelemetry-collector",
            ),
        )
        for model, app in targets:
            for endpoint, offer in zip(endpoints, offer_urls, strict=True):
                offer_ref = f"{_controller_name(ext)}:{offer}"
                result = run_in_vm(
                    primary.name,
                    f"juju integrate -m {model} {app}:{endpoint} {offer_ref}",
                    stream=False,
                    timeout=120,
                )
                if not result.ok:
                    log.warning(
                        "Integration %s %s:%s -> %s: %s",
                        model,
                        app,
                        endpoint,
                        offer_ref,
                        result.stdout[-200:],
                    )


def _report_dashboard_url(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Extract and log the external COS Grafana dashboard URL."""
    ext = cfg.observability.external
    with mon.run_step(PHASE, "dashboard-url", "Get external Grafana URL"):
        if _is_separate(ext):
            run_in_vm(
                primary.name, f"juju switch {ext.controller}", stream=False
            )
        result = run_in_vm(
            primary.name,
            f"juju run -m {ext.model} grafana/leader get-admin-password "
            "--format json",
            stream=False,
            timeout=180,
        )
        url = _parse_grafana_url(result)
        log.info("External Grafana dashboard URL: %s", url)
        log.info(
            "Retrieve the Grafana admin password with: "
            f"juju run -m {ext.model} grafana/leader get-admin-password"
        )


def _verify_features(
    cfg: DeployConfig,
    mon: DeploymentMonitor,
    primary: ComputeNode,
) -> None:
    """Push and run the verification script against the external COS model."""
    ext = cfg.observability.external
    with mon.run_step(
        PHASE, "verify-features", "Verify external observability features"
    ):
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
            f"python3 {_VERIFY_VM_PATH} --model {ext.model} "
            f"--timeout {script_timeout}",
            timeout=cfg.timeouts.observability_verify,
        )
        for line in result.stdout.splitlines():
            if line.startswith(("PASS", "FAIL")):
                log.info("%s", line)
        if not result.ok:
            raise RuntimeError(
                "External observability feature verification failed: "
                f"{result.stdout[-1500:]}"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(output: str) -> dict:
    """Parse the first JSON object in *output*, ignoring stderr noise."""
    start = output.find("{")
    if start == -1:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(output[start:])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _controller_exists(primary: ComputeNode, controller: str) -> bool:
    """Return True when *controller* is already registered locally."""
    result = run_in_vm(
        primary.name,
        "juju controllers --format json",
        stream=False,
        timeout=120,
    )
    controllers = _parse_json(result.stdout).get("controllers", {})
    return controller in controllers


def _k8s_cloud(primary: ComputeNode) -> str:
    """Return the name of the kubernetes cloud on the controller."""
    result = run_in_vm(
        primary.name,
        "juju clouds --format json",
        stream=False,
        timeout=120,
    )
    if not result.ok:
        raise RuntimeError(
            f"Failed to list controller clouds: {result.stdout[-500:]}"
        )
    clouds = _parse_json(result.stdout)
    for name, info in clouds.items():
        if isinstance(info, dict) and info.get("type") in ("k8s", "kubernetes"):
            return name
    raise RuntimeError("No kubernetes cloud found on the controller")


def _model_exists(primary: ComputeNode, model: str) -> bool:
    """Return True when *model* already exists on the current controller."""
    result = run_in_vm(
        primary.name, "juju models --format json", stream=False, timeout=120
    )
    if not result.ok:
        return False
    models = _parse_json(result.stdout).get("models", [])
    return any(
        m.get("short-name") == model or m.get("name", "").endswith(model)
        for m in models
    )


def _full_model_name(primary: ComputeNode, short_name: str) -> str:
    """Return the ``owner/model`` name for *short_name*, falling back."""
    result = run_in_vm(
        primary.name, "juju models --format json", stream=False, timeout=120
    )
    models = _parse_json(result.stdout).get("models", []) if result.ok else []
    for m in models:
        if m.get("short-name") == short_name:
            return m.get("name", short_name)
    return short_name


def _model_owner(primary: ComputeNode, model: str) -> str:
    """Return the controller account that owns *model* (e.g. ``bm0.res``)."""
    result = run_in_vm(
        primary.name, "juju models --format json", stream=False, timeout=120
    )
    models = _parse_json(result.stdout).get("models", []) if result.ok else []
    for m in models:
        if m.get("short-name") == model and "/" in (m.get("name") or ""):
            return m["name"].split("/")[0]
    return "admin"


def _parse_offer_url(stdout: str) -> str | None:
    """Extract the offer URL from ``juju offer`` output."""
    match = re.search(r'available at "([^"]+)"', stdout)
    return match.group(1) if match else None


def _parse_grafana_url(result: CommandResult) -> str:
    """Extract the Grafana URL from a get-admin-password result."""
    stdout = result.stdout
    lines = stdout.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), 0)
    try:
        data = json.loads("\n".join(lines[start:]))
        results = next(iter(data.values()))["results"]
        url = results["url"]
    except (ValueError, KeyError, StopIteration) as exc:
        raise RuntimeError(
            f"Unexpected get-admin-password output: {exc}"
        ) from exc
    if not isinstance(url, str) or not re.match(r"https?://", url):
        raise RuntimeError(f"Grafana URL is missing or invalid: {url!r}")
    return url
