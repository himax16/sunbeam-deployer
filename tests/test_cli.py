"""Tests for sunbeam_deployer.cli — CLI parsing and config overrides."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from sunbeam_deployer.cli import apply_cli_overrides, cli
from sunbeam_deployer.config import load_config

# ---------------------------------------------------------------------------
# CLI parsing tests (help-only, no live deploy)
# ---------------------------------------------------------------------------


class TestCliParsing:
    """Verify CLI structure and option acceptance via --help."""

    def test_help(self) -> None:
        """Top-level --help shows commands."""
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Commands" in result.output
        assert "deploy" in result.output
        assert "list-jobs" in result.output

    def test_no_args_shows_help(self) -> None:
        """No subcommand -> help displayed, exit 2 (missing command)."""
        result = CliRunner().invoke(cli)
        assert result.exit_code == 2
        assert "Commands" in result.output

    def test_version(self) -> None:
        """--version shows version and exits 0."""
        result = CliRunner().invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "version" in result.output.lower()

    def test_deploy_help(self) -> None:
        """deploy --help shows deploy options with groups."""
        result = CliRunner().invoke(cli, ["deploy", "--help"])
        assert result.exit_code == 0
        assert "--phase" in result.output
        assert "--testflinger" in result.output
        assert "--snap-channel" in result.output
        assert "--cancel-on-failure" in result.output

    def test_list_jobs_help(self) -> None:
        """list-jobs --help shows its options."""
        result = CliRunner().invoke(cli, ["list-jobs", "--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output
        assert "--all" in result.output
        assert "--format" in result.output

    def test_invalid_deploy_mode_exits(self) -> None:
        """Invalid --deploy-mode value -> click usage error."""
        result = CliRunner().invoke(cli, ["deploy", "--deploy-mode", "bad"])
        assert result.exit_code == 2

    def test_invalid_format_exits(self) -> None:
        """Invalid --format value in list-jobs -> click usage error."""
        result = CliRunner().invoke(cli, ["list-jobs", "--format", "bad"])
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# apply_cli_overrides (uses dict instead of argparse.Namespace)
# ---------------------------------------------------------------------------


class TestApplyCliOverrides:
    def _make_args(self, **kwargs: object) -> dict:
        """Build a minimal CLI args dict with defaults."""
        defaults: dict = dict(
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
            tf_arg=None,
            verbose=False,
        )
        defaults.update(kwargs)
        return defaults

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
        args = self._make_args(tf_arg=("-var=x",))
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

    def test_tf_job_file_enables_and_sets(self) -> None:
        cfg = load_config(None)
        args = self._make_args(tf_job_file="/path/to/job.yaml")
        apply_cli_overrides(cfg, args)
        assert cfg.testflinger.enabled is True
        assert cfg.testflinger.job_file == "/path/to/job.yaml"

    def test_tf_ssh_key_sets_path(self) -> None:
        cfg = load_config(None)
        args = self._make_args(tf_ssh_key="/path/to/key")
        apply_cli_overrides(cfg, args)
        assert cfg.testflinger.ssh_key_path == "/path/to/key"

    def test_snap_revision_sets_source(self) -> None:
        cfg = load_config(None)
        args = self._make_args(snap_revision="42")
        apply_cli_overrides(cfg, args)
        assert cfg.snap.revision == "42"
        assert cfg.snap.source == "store"

    def test_repo_dir_override(self) -> None:
        cfg = load_config(None)
        args = self._make_args(repo_dir="/custom/dir")
        apply_cli_overrides(cfg, args)
        assert cfg.repo_dir == "/custom/dir"


# ---------------------------------------------------------------------------
# main — deploy subcommand integration tests
# ---------------------------------------------------------------------------


class TestMainTestflingerAutoEnable:
    @patch("sunbeam_deployer.cli.testflinger")
    @patch("sunbeam_deployer.cli.setup_logging")
    def test_phase_testflinger_auto_enables(
        self,
        mock_logging: MagicMock,
        mock_tf_mod: MagicMock,
    ) -> None:
        """deploy --phase testflinger enables testflinger by default."""
        mock_logging.return_value = MagicMock()
        mock_tf_mod.run_phase = MagicMock()
        mock_tf_mod.PHASE = "testflinger"

        cfg = load_config(None)
        cfg.testflinger.enabled = False
        cfg.device_ip = None
        cfg.testflinger.ssh_keys = ["lp:test-key"]

        with patch("sunbeam_deployer.cli.load_config", return_value=cfg):
            result = CliRunner().invoke(
                cli, ["deploy", "--phase", "testflinger"]
            )

        assert result.exit_code == 0
        assert cfg.testflinger.enabled is True

    @patch("sunbeam_deployer.cli.setup_logging")
    def test_phase_testflinger_not_enabled_with_device_ip(
        self,
        mock_logging: MagicMock,
    ) -> None:
        """deploy --phase testflinger --device-ip does NOT auto-enable."""
        mock_logging.return_value = MagicMock()

        cfg = load_config(None)
        cfg.testflinger.enabled = False
        cfg.device_ip = "10.0.0.1"

        with (
            patch("sunbeam_deployer.cli.load_config", return_value=cfg),
            patch(
                "sunbeam_deployer.cli.wait_for_ssh",
                return_value=False,
            ),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "deploy",
                    "--phase",
                    "testflinger",
                    "--device-ip",
                    "10.0.0.1",
                ],
            )

        assert cfg.testflinger.enabled is False
        assert result.exit_code == 1


class TestMainInvalidPhase:
    @patch("sunbeam_deployer.cli.setup_logging")
    def test_invalid_phase_returns_error(self, mock_logging: MagicMock) -> None:
        """deploy --phase badphase returns exit code 1."""
        mock_logging.return_value = MagicMock()

        cfg = load_config(None)
        cfg.testflinger.enabled = False

        with patch("sunbeam_deployer.cli.load_config", return_value=cfg):
            result = CliRunner().invoke(cli, ["deploy", "--phase", "badphase"])

        assert result.exit_code == 1
