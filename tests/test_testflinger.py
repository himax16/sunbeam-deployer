"""Tests for sunbeam_deployer.phases.testflinger."""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest
import yaml

from sunbeam_deployer.config import load_config
from sunbeam_deployer.executor import CommandResult
from sunbeam_deployer.monitor import DeploymentMonitor
from sunbeam_deployer.phases.testflinger import (
    _generate_job_yaml,
    _get_device_ip,
    _submit_job,
    cancel_job,
    get_job_results,
    get_job_status,
    run_phase,
)


def _render(obj: object) -> str:
    """Render a rich object to plain text for assertions."""
    from rich.console import Console

    console = Console()
    with console.capture() as capture:
        console.print(obj)
    return capture.get()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides):
    """Build a DeployConfig with testflinger enabled."""
    cfg = load_config(None)
    cfg.testflinger.enabled = True
    cfg.testflinger.queue = "openstack"
    cfg.testflinger.distro = "noble"
    cfg.testflinger.ssh_keys = ["ssh-rsa AAAA testkey"]
    for k, v in overrides.items():
        setattr(cfg.testflinger, k, v)
    return cfg


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="", duration=0.1)


def _fail(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=1, stdout=stdout, stderr="", duration=0.1)


# ---------------------------------------------------------------------------
# _generate_job_yaml — SSH key handling
# ---------------------------------------------------------------------------


class TestGenerateJobYaml:
    def test_config_ssh_keys_only(self, tmp_path: str) -> None:
        """Config SSH keys appear in the generated YAML."""
        cfg = _make_cfg(ssh_keys=["ssh-rsa KEY1"])
        with patch(
            "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
            return_value=str(tmp_path),
        ):
            path = _generate_job_yaml(cfg, additional_ssh_keys=[])
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["reserve_data"]["ssh_keys"] == ["ssh-rsa KEY1"]

    def test_additional_ssh_keys_only(self, tmp_path: str) -> None:
        """Additional (user-prompted) SSH keys appear in the YAML."""
        cfg = _make_cfg(ssh_keys=[])
        with patch(
            "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
            return_value=str(tmp_path),
        ):
            path = _generate_job_yaml(
                cfg,
                additional_ssh_keys=["ssh-ed25519 PROMPTED"],
            )
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["reserve_data"]["ssh_keys"] == ["ssh-ed25519 PROMPTED"]

    def test_merged_ssh_keys(self, tmp_path: str) -> None:
        """Both additional and config SSH keys are merged."""
        cfg = _make_cfg(ssh_keys=["ssh-rsa CONFIG"])
        with patch(
            "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
            return_value=str(tmp_path),
        ):
            path = _generate_job_yaml(
                cfg,
                additional_ssh_keys=["ssh-ed25519 PROMPTED"],
            )
        with open(path) as f:
            data = yaml.safe_load(f)
        keys = data["reserve_data"]["ssh_keys"]
        assert "ssh-ed25519 PROMPTED" in keys
        assert "ssh-rsa CONFIG" in keys
        # Additional keys appear first
        assert keys[0] == "ssh-ed25519 PROMPTED"

    def test_no_ssh_keys_raises(self, tmp_path: str) -> None:
        """RuntimeError when both sources are empty."""
        cfg = _make_cfg(ssh_keys=[])
        with (
            patch(
                "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
                return_value=str(tmp_path),
            ),
            pytest.raises(
                RuntimeError,
                match="At least one SSH key is required",
            ),
        ):
            _generate_job_yaml(cfg, additional_ssh_keys=[])

    def test_multiple_additional_keys(self, tmp_path: str) -> None:
        """Multiple prompted keys are all included."""
        cfg = _make_cfg(ssh_keys=[])
        with patch(
            "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
            return_value=str(tmp_path),
        ):
            path = _generate_job_yaml(
                cfg,
                additional_ssh_keys=["key1", "key2", "key3"],
            )
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["reserve_data"]["ssh_keys"] == [
            "key1",
            "key2",
            "key3",
        ]

    def test_yaml_structure(self, tmp_path: str) -> None:
        """Generated YAML has the expected top-level structure."""
        cfg = _make_cfg(
            queue="my-queue",
            distro="jammy",
            reserve_timeout=1000,
        )
        with patch(
            "sunbeam_deployer.phases.testflinger._snap_tmp_dir",
            return_value=str(tmp_path),
        ):
            path = _generate_job_yaml(cfg, additional_ssh_keys=[])
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["job_queue"] == "my-queue"
        assert data["provision_data"]["distro"] == "jammy"
        assert data["reserve_data"]["timeout"] == 1000
        assert data["global_timeout"] == 1000 + 3600


# ---------------------------------------------------------------------------
# _submit_job — SSH key prompting
# ---------------------------------------------------------------------------


class TestSubmitJobSshPrompting:
    @patch("sunbeam_deployer.phases.testflinger.run_local")
    @patch("sunbeam_deployer.phases.testflinger._generate_job_yaml")
    def test_no_prompt_when_config_has_keys(
        self, mock_gen: MagicMock, mock_run: MagicMock
    ) -> None:
        """No input() call when config already has ssh_keys."""
        cfg = _make_cfg(ssh_keys=["ssh-rsa EXISTING"])
        mon = DeploymentMonitor()
        mon.add_phase("testflinger")
        mon.start_phase("testflinger")

        mock_gen.return_value = "/tmp/job.yaml"
        mock_run.return_value = _ok("job-id-123")

        with patch("builtins.open", mock_open(read_data="yaml: data")):
            job_id = _submit_job(cfg, mon)

        assert job_id == "job-id-123"
        # _generate_job_yaml should be called with empty additional keys
        mock_gen.assert_called_once_with(cfg, additional_ssh_keys=[])

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    @patch("sunbeam_deployer.phases.testflinger._generate_job_yaml")
    @patch("builtins.input", side_effect=["ssh-rsa PROMPTED", ""])
    def test_prompts_when_no_config_keys(
        self,
        mock_input: MagicMock,
        mock_gen: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Prompts for SSH keys when config has none."""
        cfg = _make_cfg(ssh_keys=[])
        mon = DeploymentMonitor()
        mon.add_phase("testflinger")
        mon.start_phase("testflinger")

        mock_gen.return_value = "/tmp/job.yaml"
        mock_run.return_value = _ok("job-id-456")

        with patch("builtins.open", mock_open(read_data="yaml: data")):
            job_id = _submit_job(cfg, mon)

        assert job_id == "job-id-456"
        mock_gen.assert_called_once_with(
            cfg, additional_ssh_keys=["ssh-rsa PROMPTED"]
        )

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    @patch("sunbeam_deployer.phases.testflinger._generate_job_yaml")
    @patch(
        "builtins.input",
        side_effect=["ssh-rsa KEY1", "ssh-ed25519 KEY2", ""],
    )
    def test_prompts_multiple_keys(
        self,
        mock_input: MagicMock,
        mock_gen: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Collects multiple prompted keys until empty input."""
        cfg = _make_cfg(ssh_keys=[])
        mon = DeploymentMonitor()
        mon.add_phase("testflinger")
        mon.start_phase("testflinger")

        mock_gen.return_value = "/tmp/job.yaml"
        mock_run.return_value = _ok("job-multi")

        with patch("builtins.open", mock_open(read_data="yaml: data")):
            _submit_job(cfg, mon)

        mock_gen.assert_called_once_with(
            cfg,
            additional_ssh_keys=["ssh-rsa KEY1", "ssh-ed25519 KEY2"],
        )

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    @patch("sunbeam_deployer.phases.testflinger._generate_job_yaml")
    @patch("builtins.input", side_effect=[""])
    def test_prompt_immediate_empty_gives_no_keys(
        self,
        mock_input: MagicMock,
        mock_gen: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        """Immediate empty input results in no additional keys."""
        cfg = _make_cfg(ssh_keys=[])
        mon = DeploymentMonitor()
        mon.add_phase("testflinger")
        mon.start_phase("testflinger")

        mock_gen.return_value = "/tmp/job.yaml"
        mock_run.return_value = _ok("job-empty")

        with patch("builtins.open", mock_open(read_data="yaml: data")):
            _submit_job(cfg, mon)

        mock_gen.assert_called_once_with(cfg, additional_ssh_keys=[])

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    @patch("sunbeam_deployer.phases.testflinger._generate_job_yaml")
    def test_uses_job_file_when_set(
        self, mock_gen: MagicMock, mock_run: MagicMock
    ) -> None:
        """When job_file is configured, skip generation and prompting."""
        cfg = _make_cfg(job_file="/existing/job.yaml")
        mon = DeploymentMonitor()
        mon.add_phase("testflinger")
        mon.start_phase("testflinger")

        mock_run.return_value = _ok("job-from-file")

        with patch("builtins.open", mock_open(read_data="yaml: data")):
            job_id = _submit_job(cfg, mon)

        assert job_id == "job-from-file"
        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# cancel_job / get_job_status / get_job_results
# ---------------------------------------------------------------------------


class TestCancelJob:
    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_cancel_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok()
        cancel_job("abc-123")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["testflinger", "cancel", "abc-123"]

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_cancel_failure_does_not_raise(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail("not found")
        cancel_job("bad-id")  # Should not raise


class TestGetJobStatus:
    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_returns_status(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("Reserve\n")
        assert get_job_status("id") == "reserve"

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_returns_empty_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail()
        assert get_job_status("id") == ""


class TestGetJobResults:
    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_parses_json(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok('{"device_info": {"device_ip": "1.2.3.4"}}')
        result = get_job_results("id")
        assert result["device_info"]["device_ip"] == "1.2.3.4"

    @patch("sunbeam_deployer.phases.testflinger.run_local")
    def test_raises_on_bad_json(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("not json")
        with pytest.raises(RuntimeError, match="Could not parse"):
            get_job_results("id")


# ---------------------------------------------------------------------------
# _get_device_ip
# ---------------------------------------------------------------------------


class TestGetDeviceIp:
    @patch("sunbeam_deployer.phases.testflinger.get_job_results")
    def test_returns_ip(self, mock_results: MagicMock) -> None:
        mock_results.return_value = {
            "device_info": {"device_ip": "10.0.0.5", "agent_name": "ag1"}
        }
        assert _get_device_ip("job-1") == "10.0.0.5"

    @patch("sunbeam_deployer.phases.testflinger.time.sleep")
    @patch("sunbeam_deployer.phases.testflinger.get_job_results")
    def test_retries_then_finds_ip(
        self,
        mock_results: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_results.side_effect = [
            {"device_info": {}},
            {"device_info": {"device_ip": "10.0.0.6"}},
        ]
        assert _get_device_ip("job-2", retries=3) == "10.0.0.6"

    @patch("sunbeam_deployer.phases.testflinger.time.sleep")
    @patch("sunbeam_deployer.phases.testflinger.get_job_results")
    def test_raises_after_all_retries(
        self,
        mock_results: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_results.return_value = {"device_info": {}}
        with pytest.raises(RuntimeError, match="No device_ip"):
            _get_device_ip("job-3", retries=2)


# ---------------------------------------------------------------------------
# run_phase — integration
# ---------------------------------------------------------------------------


class TestRunPhase:
    @patch("sunbeam_deployer.phases.testflinger._setup_ssh")
    @patch("sunbeam_deployer.phases.testflinger._wait_for_ready")
    def test_attach_existing_job(
        self,
        mock_wait: MagicMock,
        mock_ssh: MagicMock,
    ) -> None:
        """Attaching to an existing job skips submit."""
        cfg = _make_cfg(job_id="existing-job")
        mon = DeploymentMonitor()
        mock_wait.return_value = "10.0.0.1"

        ip = run_phase(cfg, mon)

        assert ip == "10.0.0.1"
        mock_wait.assert_called_once()
        assert cfg.testflinger.job_id == "existing-job"

    @patch("sunbeam_deployer.phases.testflinger._setup_ssh")
    @patch("sunbeam_deployer.phases.testflinger._wait_for_ready")
    @patch("sunbeam_deployer.phases.testflinger._submit_job")
    def test_submit_new_job(
        self,
        mock_submit: MagicMock,
        mock_wait: MagicMock,
        mock_ssh: MagicMock,
    ) -> None:
        """Submitting a new job stores the job_id in config."""
        cfg = _make_cfg(job_id=None)
        mon = DeploymentMonitor()
        mock_submit.return_value = "new-job-id"
        mock_wait.return_value = "10.0.0.2"

        ip = run_phase(cfg, mon)

        assert ip == "10.0.0.2"
        assert cfg.testflinger.job_id == "new-job-id"

    @patch("sunbeam_deployer.phases.testflinger._setup_ssh")
    @patch("sunbeam_deployer.phases.testflinger._wait_for_ready")
    def test_failure_marks_phase_failed(
        self,
        mock_wait: MagicMock,
        mock_ssh: MagicMock,
    ) -> None:
        """Phase is marked FAILED on exception."""
        cfg = _make_cfg(job_id="fail-job")
        mon = DeploymentMonitor()
        mock_wait.side_effect = RuntimeError("provision timeout")

        with pytest.raises(RuntimeError, match="provision timeout"):
            run_phase(cfg, mon)

        summary = _render(mon.summary())
        assert "FAILED" in summary
