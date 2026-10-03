"""Configuration loading, validation, and defaults."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "deploy_mode": "manual",
    "repo_url": "https://github.com/himax16/sunbeam-proxified-dev.git",
    "repo_branch": "main",
    "repo_dir": "~/sunbeam-proxified-dev",
    "testflinger": {
        "enabled": False,
        "job_id": None,
        "job_file": None,
        "queue": "openstack",
        "distro": "noble",
        "reserve_timeout": 259200,
        "ssh_keys": [],
        "ssh_user": "ubuntu",
        "ssh_key_path": None,
        "provision_timeout": 1800,
    },
    "snap": {
        "source": "store",
        "channel": "2024.1/edge",
        "revision": None,
        "local_path": None,
        "install_method": "dangerous",
    },
    "sunbeam": {
        "roles": ["control", "compute", "storage"],
        "node_roles": {},
        "manifest": True,
        "accept_defaults": False,
        "bootstrap_extra_args": [],
        "cluster_node_count": 0,
        "resize_topology": "auto",
        "manifest_overrides": None,
    },
    "observability": {
        "enabled": False,
        "channel": "1/stable",
        "storage": {
            "prometheus": "20G",
            "loki_index": "2G",
            "loki_chunks": "5G",
            "grafana": "1G",
            "alertmanager": "1G",
        },
        "external": {
            "enabled": False,
            "controller": None,
            "model": "external-cos",
            "channel": "latest/stable",
        },
    },
    "terraform": {
        "extra_args": [],
        "bootstrap_retries": 1,
        "vm_boot_timeout": "15m",
    },
    "logging": {
        "log_dir": "~/.local/share/sunbeam-deployer/logs",
        "verbose": False,
    },
    "timeouts": {
        "cloud_init_wait": 600,
        "snap_install": 600,
        "prepare_node": 600,
        "cluster_bootstrap": 7200,
        "cluster_join": 3600,
        "cluster_resize": 7200,
        "terraform_apply": 3600,
        "observability_enable": 5400,
        "observability_verify": 1800,
    },
    "concurrency": {
        "vm_deploy": 2,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SnapConfig:
    source: str  # "store" | "local"
    channel: str
    revision: str | None
    local_path: str | None
    install_method: str  # "dangerous" | "try"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.source not in ("store", "local"):
            errors.append(
                f"snap.source must be 'store' or 'local', got '{self.source}'"
            )
        if self.source == "local":
            if not self.local_path:
                errors.append(
                    "snap.local_path is required when snap.source='local'"
                )
            elif not Path(os.path.expanduser(self.local_path)).exists():
                errors.append(
                    f"snap.local_path does not exist: {self.local_path}"
                )
        if self.install_method not in ("dangerous", "try"):
            errors.append(
                "snap.install_method must be "
                "'dangerous' or 'try', "
                f"got '{self.install_method}'"
            )
        return errors


@dataclass
class SunbeamConfig:
    roles: list[str]
    node_roles: dict[str, list[str]]
    manifest: bool
    accept_defaults: bool
    bootstrap_extra_args: list[str]
    cluster_node_count: int
    resize_topology: str = "auto"
    manifest_overrides: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if (
            not isinstance(self.cluster_node_count, int)
            or self.cluster_node_count < 0
        ):
            errors.append(
                "sunbeam.cluster_node_count must be a"
                " non-negative integer (0 = all nodes)"
            )
        if self.resize_topology not in ("auto", "single", "multi", "large"):
            errors.append(
                "sunbeam.resize_topology must be one of "
                "'auto', 'single', 'multi', 'large', "
                f"got '{self.resize_topology}'"
            )
        if self.manifest_overrides:
            p = Path(os.path.expanduser(self.manifest_overrides))
            if not p.is_file():
                errors.append(
                    "sunbeam.manifest_overrides does not exist or is not a "
                    f"file: {self.manifest_overrides}"
                )
        return errors


_STORAGE_SIZE_RE = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|K|M|G|T|P)$")


@dataclass
class ObservabilityStorageConfig:
    prometheus: str = "20G"
    loki_index: str = "2G"
    loki_chunks: str = "5G"
    grafana: str = "1G"
    alertmanager: str = "1G"

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name in (
            "prometheus",
            "loki_index",
            "loki_chunks",
            "grafana",
            "alertmanager",
        ):
            v = getattr(self, name)
            if not _STORAGE_SIZE_RE.match(str(v)):
                errors.append(
                    f"observability.storage.{name}: invalid size '{v}' "
                    "(e.g. 20G, 512Mi)"
                )
        return errors


@dataclass
class ExternalObservabilityConfig:
    enabled: bool = False
    controller: str | None = None  # None = cluster's own controller
    model: str = "external-cos"
    channel: str = "latest/stable"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.enabled:
            return errors
        if not self.model:
            errors.append("observability.external.model cannot be empty")
        if not self.channel:
            errors.append("observability.external.channel cannot be empty")
        return errors


@dataclass
class ObservabilityConfig:
    enabled: bool = False
    channel: str = "1/stable"
    storage: ObservabilityStorageConfig = field(
        default_factory=ObservabilityStorageConfig
    )
    external: ExternalObservabilityConfig = field(
        default_factory=ExternalObservabilityConfig
    )

    def validate(self) -> list[str]:
        errors: list[str] = self.storage.validate() if self.enabled else []
        errors.extend(self.external.validate())
        return errors


@dataclass
class TerraformConfig:
    extra_args: list[str]
    bootstrap_retries: int
    vm_boot_timeout: str


@dataclass
class LoggingConfig:
    log_dir: str
    verbose: bool


@dataclass
class TimeoutsConfig:
    cloud_init_wait: int
    snap_install: int
    prepare_node: int
    cluster_bootstrap: int
    cluster_join: int
    cluster_resize: int
    terraform_apply: int
    observability_enable: int
    observability_verify: int


@dataclass
class TestflingerConfig:
    __test__ = False

    enabled: bool
    job_id: str | None
    job_file: str | None
    queue: str
    distro: str
    reserve_timeout: int
    ssh_keys: list[str]
    ssh_user: str
    ssh_key_path: str | None
    provision_timeout: int

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.enabled:
            return errors
        if self.job_id and self.job_file:
            errors.append(
                "Testflinger: specify either job_id (attach)"
                " or job_file (submit), not both"
            )
        # Check for SSH keys if submitting a new job (job_id is not set)
        if not self.job_id and not self.job_file and not self.ssh_keys:
            errors.append(
                "Testflinger: ssh_keys are required when submitting a new job"
            )
        return errors


@dataclass
class ConcurrencyConfig:
    vm_deploy: int


@dataclass
class DeployConfig:
    deploy_mode: str
    repo_url: str
    repo_branch: str
    repo_dir: str
    testflinger: TestflingerConfig
    snap: SnapConfig
    sunbeam: SunbeamConfig
    terraform: TerraformConfig
    logging: LoggingConfig
    timeouts: TimeoutsConfig
    concurrency: ConcurrencyConfig
    observability: ObservabilityConfig
    device_ip: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.deploy_mode not in ("manual", "maas"):
            errors.append(
                "deploy_mode must be 'manual' or 'maas',"
                f" got '{self.deploy_mode}'"
            )
        if self.deploy_mode == "maas":
            errors.append("MAAS mode is not yet supported — use 'manual'")
        errors.extend(self.testflinger.validate())
        errors.extend(self.snap.validate())
        errors.extend(self.sunbeam.validate())
        errors.extend(self.observability.validate())
        return errors


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> DeployConfig:
    """Load configuration from a YAML file, merged with defaults.

    If *path* is ``None`` the default configuration is returned.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    merged_config = deep_merge(_DEFAULTS, raw)

    tf_raw = merged_config["testflinger"]
    if tf_raw.get("job_file"):
        tf_raw["job_file"] = os.path.expanduser(tf_raw["job_file"])
    if tf_raw.get("ssh_key_path"):
        tf_raw["ssh_key_path"] = os.path.expanduser(tf_raw["ssh_key_path"])

    obs_raw = merged_config["observability"]
    obs_raw["storage"] = ObservabilityStorageConfig(
        **obs_raw.get("storage", {})
    )
    obs_raw["external"] = ExternalObservabilityConfig(
        **obs_raw.get("external", {})
    )

    cfg = DeployConfig(
        deploy_mode=merged_config["deploy_mode"],
        repo_url=merged_config["repo_url"],
        repo_branch=merged_config["repo_branch"],
        # Keep ~ unexpanded — the remote shell expands it when running via SSH.
        # For local execution the shell in subprocess also expands ~.
        repo_dir=merged_config["repo_dir"],
        testflinger=TestflingerConfig(**tf_raw),
        snap=SnapConfig(**merged_config["snap"]),
        sunbeam=SunbeamConfig(**merged_config["sunbeam"]),
        terraform=TerraformConfig(**merged_config["terraform"]),
        logging=LoggingConfig(**merged_config["logging"]),
        timeouts=TimeoutsConfig(**merged_config["timeouts"]),
        concurrency=ConcurrencyConfig(**merged_config["concurrency"]),
        observability=ObservabilityConfig(**obs_raw),
    )

    errors = cfg.validate()
    if errors:
        raise ValueError("Configuration errors:\n  " + "\n  ".join(errors))

    return cfg
