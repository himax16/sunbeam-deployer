"""Tests for config — defaults, merging, loading, validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sunbeam_deployer.config import (
    DeployConfig,
    SnapConfig,
    TestflingerConfig,
    _deep_merge,
    load_config,
)

# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_simple_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_nested_merge(self) -> None:
        base = {"x": {"y": 1, "z": 2}}
        override = {"x": {"z": 99}}
        result = _deep_merge(base, override)
        assert result == {"x": {"y": 1, "z": 99}}

    def test_add_new_key(self) -> None:
        base = {"a": 1}
        override = {"b": 2}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self) -> None:
        base = {"x": {"y": 1}}
        override = {"x": {"y": 2}}
        _deep_merge(base, override)
        assert base == {"x": {"y": 1}}

    def test_override_replaces_dict_with_scalar(self) -> None:
        base = {"x": {"nested": True}}
        override = {"x": "flat"}
        result = _deep_merge(base, override)
        assert result == {"x": "flat"}

    def test_override_replaces_scalar_with_dict(self) -> None:
        base = {"x": "flat"}
        override = {"x": {"nested": True}}
        result = _deep_merge(base, override)
        assert result == {"x": {"nested": True}}

    def test_empty_override(self) -> None:
        base = {"a": 1, "b": {"c": 2}}
        result = _deep_merge(base, {})
        assert result == base

    def test_empty_base(self) -> None:
        override = {"a": 1}
        result = _deep_merge({}, override)
        assert result == {"a": 1}

    def test_deeply_nested(self) -> None:
        base = {"a": {"b": {"c": {"d": 1, "e": 2}}}}
        override = {"a": {"b": {"c": {"e": 99}}}}
        result = _deep_merge(base, override)
        assert result == {"a": {"b": {"c": {"d": 1, "e": 99}}}}


# ---------------------------------------------------------------------------
# load_config — defaults
# ---------------------------------------------------------------------------


class TestLoadConfigDefaults:
    def test_no_file_returns_valid_config(self) -> None:
        cfg = load_config(None)
        assert isinstance(cfg, DeployConfig)

    def test_default_deploy_mode(self) -> None:
        cfg = load_config(None)
        assert cfg.deploy_mode == "manual"

    def test_default_repo_dir_keeps_tilde(self) -> None:
        cfg = load_config(None)
        assert cfg.repo_dir == "~/sunbeam-proxified-dev"

    def test_default_snap_config(self) -> None:
        cfg = load_config(None)
        assert cfg.snap.source == "store"
        assert cfg.snap.channel == "2024.1/edge"
        assert cfg.snap.revision is None

    def test_default_sunbeam_roles(self) -> None:
        cfg = load_config(None)
        assert cfg.sunbeam.roles == ["control", "compute", "storage"]

    def test_default_node_roles_empty(self) -> None:
        cfg = load_config(None)
        assert cfg.sunbeam.node_roles == {}

    def test_default_timeouts(self) -> None:
        cfg = load_config(None)
        assert cfg.timeouts.cluster_bootstrap == 7200
        assert cfg.timeouts.cluster_join == 3600

    def test_default_concurrency(self) -> None:
        cfg = load_config(None)
        assert cfg.concurrency.vm_deploy == 2

    def test_testflinger_disabled_by_default(self) -> None:
        cfg = load_config(None)
        assert cfg.testflinger.enabled is False


# ---------------------------------------------------------------------------
# load_config — from YAML
# ---------------------------------------------------------------------------


class TestLoadConfigFromYAML:
    def test_partial_override(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            textwrap.dedent("""\
            snap:
              channel: 2024.2/beta
        """)
        )
        cfg = load_config(yaml_file)
        assert cfg.snap.channel == "2024.2/beta"
        assert cfg.snap.source == "store"  # preserved from defaults

    def test_node_roles_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            textwrap.dedent("""\
            sunbeam:
              node_roles:
                bm0:
                  - control
                  - compute
                bm1:
                  - compute
        """)
        )
        cfg = load_config(yaml_file)
        assert cfg.sunbeam.node_roles == {
            "bm0": ["control", "compute"],
            "bm1": ["compute"],
        }
        # Default roles still present
        assert cfg.sunbeam.roles == ["control", "compute", "storage"]

    def test_empty_yaml_returns_defaults(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("")
        cfg = load_config(yaml_file)
        assert cfg.deploy_mode == "manual"

    def test_nested_override_preserves_siblings(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            textwrap.dedent("""\
            timeouts:
              cluster_bootstrap: 9999
        """)
        )
        cfg = load_config(yaml_file)
        assert cfg.timeouts.cluster_bootstrap == 9999
        assert cfg.timeouts.cluster_join == 3600  # default preserved


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_default_config(self) -> None:
        cfg = load_config(None)
        assert cfg.validate() == []

    def test_invalid_deploy_mode(self) -> None:
        cfg = load_config(None)
        cfg.deploy_mode = "invalid"
        errors = cfg.validate()
        assert any("deploy_mode" in e for e in errors)

    def test_maas_mode_unsupported(self) -> None:
        cfg = load_config(None)
        cfg.deploy_mode = "maas"
        errors = cfg.validate()
        assert any("MAAS" in e for e in errors)

    def test_snap_invalid_source(self) -> None:
        snap = SnapConfig(
            source="invalid",
            channel="2024.1/edge",
            revision=None,
            local_path=None,
            install_method="dangerous",
        )
        errors = snap.validate()
        assert any("snap.source" in e for e in errors)

    def test_snap_local_requires_path(self) -> None:
        snap = SnapConfig(
            source="local",
            channel="2024.1/edge",
            revision=None,
            local_path=None,
            install_method="dangerous",
        )
        errors = snap.validate()
        assert any("snap.local_path" in e for e in errors)

    def test_snap_invalid_install_method(self) -> None:
        snap = SnapConfig(
            source="store",
            channel="2024.1/edge",
            revision=None,
            local_path=None,
            install_method="bad",
        )
        errors = snap.validate()
        assert any("snap.install_method" in e for e in errors)

    def test_testflinger_both_job_id_and_file_invalid(self) -> None:
        tf = TestflingerConfig(
            enabled=True,
            job_id="some-id",
            job_file="/some/file",
            queue="openstack",
            distro="noble",
            reserve_timeout=259200,
            ssh_keys=[],
            ssh_user="ubuntu",
            ssh_key_path=None,
            provision_timeout=1800,
        )
        errors = tf.validate()
        assert any("either job_id" in e for e in errors)

    def test_testflinger_submit_requires_ssh_keys(self) -> None:
        tf = TestflingerConfig(
            enabled=True,
            job_id=None,
            job_file=None,
            queue="openstack",
            distro="noble",
            reserve_timeout=259200,
            ssh_keys=[],
            ssh_user="ubuntu",
            ssh_key_path=None,
            provision_timeout=1800,
        )
        errors = tf.validate()
        assert any("ssh_keys" in e for e in errors)

    def test_testflinger_disabled_skips_validation(self) -> None:
        tf = TestflingerConfig(
            enabled=False,
            job_id="x",
            job_file="y",
            queue="openstack",
            distro="noble",
            reserve_timeout=259200,
            ssh_keys=[],
            ssh_user="ubuntu",
            ssh_key_path=None,
            provision_timeout=1800,
        )
        assert tf.validate() == []

    def test_invalid_config_raises_on_load(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("deploy_mode: invalid_mode\n")
        with pytest.raises(ValueError, match="Configuration errors"):
            load_config(yaml_file)
