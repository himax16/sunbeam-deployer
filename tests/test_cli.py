"""Tests for sunbeam_deployer.__main__ — CLI parsing and config overrides."""

from __future__ import annotations

import argparse

import pytest

from sunbeam_deployer.__main__ import apply_cli_overrides, build_parser
from sunbeam_deployer.config import load_config

# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.phase == "all"
        assert args.verbose is False
        assert args.config is None
        assert args.testflinger is None
        assert args.device_ip is None
        assert args.cancel_on_failure is False

    def test_phase_choices(self) -> None:
        parser = build_parser()
        for phase in (
            "all",
            "testflinger",
            "host-setup",
            "vm-deploy",
            "cluster",
        ):
            args = parser.parse_args(["--phase", phase])
            assert args.phase == phase

    def test_testflinger_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--testflinger"])
        assert args.testflinger is True

    def test_tf_job_id(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--tf-job-id", "abc-123"])
        assert args.tf_job_id == "abc-123"

    def test_device_ip(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--device-ip", "10.0.0.1"])
        assert args.device_ip == "10.0.0.1"

    def test_snap_overrides(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--snap-channel",
                "2024.2/beta",
                "--snap-revision",
                "42",
            ]
        )
        assert args.snap_channel == "2024.2/beta"
        assert args.snap_revision == "42"

    def test_snap_file(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--snap-file", "/path/to/snap"])
        assert args.snap_file == "/path/to/snap"

    def test_deploy_mode(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--deploy-mode", "manual"])
        assert args.deploy_mode == "manual"

    def test_invalid_deploy_mode_exits(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--deploy-mode", "bad"])

    def test_tf_args_repeatable(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--tf-arg", "foo", "--tf-arg", "bar"])
        assert args.tf_args == ["foo", "bar"]

    def test_verbose(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["-v"])
        assert args.verbose is True

    def test_accept_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--accept-defaults"])
        assert args.accept_defaults is True

    def test_no_manifest(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--no-manifest"])
        assert args.no_manifest is True

    def test_cancel_on_failure(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--cancel-on-failure"])
        assert args.cancel_on_failure is True


# ---------------------------------------------------------------------------
# apply_cli_overrides
# ---------------------------------------------------------------------------


class TestApplyCliOverrides:
    def _make_args(self, **kwargs) -> argparse.Namespace:
        """Build a minimal argparse.Namespace with defaults."""
        defaults = dict(
            testflinger=None,
            tf_job_id=None,
            tf_job_file=None,
            tf_ssh_key=None,
            device_ip=None,
            snap_channel=None,
            snap_revision=None,
            snap_file=None,
            deploy_mode=None,
            repo_dir=None,
            accept_defaults=None,
            no_manifest=False,
            tf_args=None,
            verbose=False,
        )
        defaults.update(kwargs)

        return argparse.Namespace(**defaults)

    def test_testflinger_enables(self) -> None:
        cfg = load_config(None)
        args = self._make_args(testflinger=True)
        apply_cli_overrides(cfg, args)
        assert cfg.testflinger.enabled is True

    def test_tf_job_id_enables_and_sets(self) -> None:
        cfg = load_config(None)
        args = self._make_args(tf_job_id="my-id")
        apply_cli_overrides(cfg, args)
        assert cfg.testflinger.enabled is True
        assert cfg.testflinger.job_id == "my-id"

    def test_device_ip_disables_testflinger(self) -> None:
        cfg = load_config(None)
        cfg.testflinger.enabled = True
        args = self._make_args(device_ip="10.0.0.1")
        apply_cli_overrides(cfg, args)
        assert cfg.testflinger.enabled is False
        assert cfg.device_ip == "10.0.0.1"

    def test_snap_channel_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(snap_channel="2024.2/stable")
        apply_cli_overrides(cfg, args)
        assert cfg.snap.channel == "2024.2/stable"
        assert cfg.snap.source == "store"

    def test_snap_file_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(snap_file="/my/snap.snap")
        apply_cli_overrides(cfg, args)
        assert cfg.snap.local_path == "/my/snap.snap"
        assert cfg.snap.source == "local"

    def test_deploy_mode_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(deploy_mode="manual")
        apply_cli_overrides(cfg, args)
        assert cfg.deploy_mode == "manual"

    def test_accept_defaults_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(accept_defaults=True)
        apply_cli_overrides(cfg, args)
        assert cfg.sunbeam.accept_defaults is True

    def test_no_manifest_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(no_manifest=True)
        apply_cli_overrides(cfg, args)
        assert cfg.sunbeam.manifest is False

    def test_tf_args_extend(self) -> None:
        cfg = load_config(None)
        args = self._make_args(tf_args=["-var=x"])
        apply_cli_overrides(cfg, args)
        assert "-var=x" in cfg.terraform.extra_args

    def test_verbose_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(verbose=True)
        apply_cli_overrides(cfg, args)
        assert cfg.logging.verbose is True

    def test_no_overrides_preserves_defaults(self) -> None:
        cfg = load_config(None)
        args = self._make_args()
        apply_cli_overrides(cfg, args)
        assert cfg.deploy_mode == "manual"
        assert cfg.snap.channel == "2024.1/edge"
        assert cfg.testflinger.enabled is False
