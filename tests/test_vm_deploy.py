"""Tests for sunbeam_deployer.phases.vm_deploy manifest merging."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from sunbeam_deployer.config import DeployConfig, load_config
from sunbeam_deployer.monitor import DeploymentMonitor
from sunbeam_deployer.phases import vm_deploy
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

TERRAFORM_MANIFEST = """\
core:
  config:
    microceph_config:
      osd: 1
"""


def _make_infra() -> InfraInfo:
    return InfraInfo(
        nodes=[
            ComputeNode(
                name="bm0",
                fqdn="bm0.test.local",
                hostname="bm0",
                ip="10.0.0.10",
                roles=["control", "compute", "storage"],
            )
        ],
        plan_dir="/tmp/plan",
        manifest_path="/tmp/manifest.yaml",
        ssh_private_key_path="/tmp/key",
    )


def _decode_write(cmd: str) -> dict:
    match = re.match(r"^echo (\S+) \| base64 -d", cmd)
    assert match is not None, f"unexpected write cmd: {cmd}"
    return yaml.safe_load(base64.b64decode(match.group(1)).decode())


def _cfg_with_overrides(path: Path) -> DeployConfig:
    cfg = load_config(None)
    cfg.sunbeam.manifest_overrides = str(path)
    return cfg


class TestMergeManifestOverrides:
    def test_merges_charm_channels(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.yaml"
        overrides.write_text(
            "core:\n"
            "  software:\n"
            "    charms:\n"
            "      keystone-k8s:\n"
            "        channel: 2026.1/edge\n"
        )
        cfg = _cfg_with_overrides(overrides)
        mon = DeploymentMonitor()
        mon.add_phase(vm_deploy.PHASE)
        mon.start_phase(vm_deploy.PHASE)

        writes: list[str] = []

        def _run_host(cmd: str, **kwargs: object) -> MagicMock:
            if cmd.startswith("cat "):
                return MagicMock(ok=True, stdout=TERRAFORM_MANIFEST)
            writes.append(cmd)
            return MagicMock(ok=True, stdout="")

        with patch("sunbeam_deployer.phases.vm_deploy.run_host", _run_host):
            vm_deploy._merge_manifest_overrides(cfg, mon, _make_infra())

        assert len(writes) == 1
        merged = _decode_write(writes[0])
        assert merged["core"]["software"]["charms"]["keystone-k8s"] == {
            "channel": "2026.1/edge"
        }
        # Original core config is preserved.
        assert merged["core"]["config"]["microceph_config"]["osd"] == 1

    def test_missing_host_manifest_raises(self, tmp_path: Path) -> None:
        overrides = tmp_path / "overrides.yaml"
        overrides.write_text("core: {}\n")
        cfg = _cfg_with_overrides(overrides)
        mon = DeploymentMonitor()
        mon.add_phase(vm_deploy.PHASE)
        mon.start_phase(vm_deploy.PHASE)

        with (
            patch(
                "sunbeam_deployer.phases.vm_deploy.run_host",
                return_value=MagicMock(ok=False, stdout=""),
            ),
            pytest.raises(RuntimeError),
        ):
            vm_deploy._merge_manifest_overrides(cfg, mon, _make_infra())
