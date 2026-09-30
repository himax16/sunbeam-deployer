"""Tests for sunbeam_deployer.phases.observability."""

from __future__ import annotations

import base64
import json
import re
from unittest.mock import MagicMock, patch

import pytest
import yaml

from sunbeam_deployer.config import load_config
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases import observability
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

FULL_MANIFEST = """\
core:
  config:
    microceph_config:
      osd: 1
"""

MANIFEST_WITH_OBSERVABILITY = """\
core:
  config:
    microceph_config:
      osd: 1
features:
  observability:
    embedded:
      software:
        charms:
          prometheus-k8s:
            storage:
              database: 99G
"""

ALL_ACTIVE = {app: "active" for app in observability._COS_APPS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(name: str) -> ComputeNode:
    return ComputeNode(
        name=name,
        fqdn=f"{name}.test.local",
        hostname=name,
        ip=f"10.0.0.{ord(name[-1])}",
        roles=["control", "compute", "storage"],
    )


def _make_infra(*names: str) -> InfraInfo:
    return InfraInfo(
        nodes=[_make_node(n) for n in names],
        plan_dir="/tmp/plan",
        manifest_path="/tmp/manifest.yaml",
        ssh_private_key_path="/tmp/key",
    )


def _make_cfg():
    cfg = load_config(None)
    cfg.observability.enabled = True
    return cfg


def _make_mon() -> DeploymentMonitor:
    """Monitor with the observability phase already registered.

    Step-level functions run under ``mon.run_step``, which requires the
    phase to be registered first (``run_phase`` does this in production).
    """
    mon = DeploymentMonitor()
    mon.add_phase(observability.PHASE)
    mon.start_phase(observability.PHASE)
    return mon


def _juju_status(apps: dict[str, str]) -> str:
    applications = {
        name: {"application-status": {"current": status}}
        for name, status in apps.items()
    }
    return json.dumps({"applications": applications})


def _decode_write(cmd: str) -> dict:
    """Decode the base64 manifest payload from a write ``run_host`` cmd."""
    match = re.match(r"^echo (\S+) \| base64 -d", cmd)
    assert match is not None, f"unexpected write cmd: {cmd}"
    return yaml.safe_load(base64.b64decode(match.group(1)).decode())


def _run_host_side(cmd: str, **kwargs: object) -> MagicMock:
    if cmd.startswith("cat "):
        return MagicMock(ok=True, stdout=FULL_MANIFEST)
    return MagicMock(ok=True, stdout="")


# ---------------------------------------------------------------------------
# _observability_manifest_block
# ---------------------------------------------------------------------------


class TestManifestBlock:
    def test_defaults(self) -> None:
        block = observability._observability_manifest_block(_make_cfg())
        charms = block["features"]["observability"]["embedded"]["software"][
            "charms"
        ]
        for name in (
            "prometheus-k8s",
            "loki-k8s",
            "grafana-k8s",
            "alertmanager-k8s",
        ):
            assert charms[name]["channel"] == "1/stable"
        assert charms["prometheus-k8s"]["storage"]["database"] == "20G"
        assert charms["loki-k8s"]["storage"]["active-index-directory"] == "2G"
        assert charms["loki-k8s"]["storage"]["loki-chunks"] == "5G"
        assert charms["grafana-k8s"]["storage"]["database"] == "1G"
        assert charms["alertmanager-k8s"]["storage"]["data"] == "1G"

    def test_custom_storage(self) -> None:
        cfg = _make_cfg()
        cfg.observability.storage.prometheus = "40G"
        block = observability._observability_manifest_block(cfg)
        charms = block["features"]["observability"]["embedded"]["software"][
            "charms"
        ]
        assert charms["prometheus-k8s"]["storage"]["database"] == "40G"

    def test_custom_channel(self) -> None:
        cfg = _make_cfg()
        cfg.observability.channel = "2024.1/stable"
        block = observability._observability_manifest_block(cfg)
        charms = block["features"]["observability"]["embedded"]["software"][
            "charms"
        ]
        assert charms["grafana-k8s"]["channel"] == "2024.1/stable"
        assert charms["loki-k8s"]["channel"] == "2024.1/stable"


# ---------------------------------------------------------------------------
# _configure_manifest
# ---------------------------------------------------------------------------


class TestConfigureManifest:
    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_injects_block_into_complete_manifest(
        self, mock_run: MagicMock
    ) -> None:
        mock_run.side_effect = _run_host_side
        cfg = _make_cfg()
        mon = _make_mon()
        infra = _make_infra("bm0")
        primary = infra.nodes[0]

        observability._configure_manifest(cfg, mon, infra, primary)

        calls = mock_run.call_args_list
        assert calls[0].args[0] == "cat /tmp/manifest.yaml"
        payload = _decode_write(calls[1].args[0])
        assert payload["core"] == {"config": {"microceph_config": {"osd": 1}}}
        charms = payload["features"]["observability"]["embedded"]["software"][
            "charms"
        ]
        assert charms["prometheus-k8s"]["storage"]["database"] == "20G"
        assert "bm0/home/ubuntu/manifest.yaml" in calls[2].args[0]

    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_cat_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, stdout="")
        cfg = _make_cfg()
        mon = _make_mon()
        infra = _make_infra("bm0")

        with pytest.raises(RuntimeError, match="Original manifest not found"):
            observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_missing_core_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, stdout="software: {}\n"),
            MagicMock(ok=True, stdout=""),
        ]
        cfg = _make_cfg()
        mon = _make_mon()
        infra = _make_infra("bm0")

        with pytest.raises(RuntimeError, match="looks incomplete"):
            observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_no_manifest_skips_cat(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = _run_host_side
        cfg = _make_cfg()
        cfg.sunbeam.manifest = False
        mon = _make_mon()
        infra = _make_infra("bm0")

        observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

        calls = mock_run.call_args_list
        # First call is the write (no cat), and its payload is only the
        # observability block.
        assert not any(c.args[0].startswith("cat ") for c in calls)
        payload = _decode_write(calls[0].args[0])
        assert "core" not in payload
        assert "features" in payload

    @patch(
        "sunbeam_deployer.phases.observability._prompt_override_observability"
    )
    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_existing_segment_override(
        self, mock_run: MagicMock, mock_prompt: MagicMock
    ) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, stdout=MANIFEST_WITH_OBSERVABILITY),
            MagicMock(ok=True, stdout=""),
            MagicMock(ok=True, stdout=""),
        ]
        mock_prompt.return_value = True
        cfg = _make_cfg()
        cfg.observability.storage.prometheus = "40G"
        mon = _make_mon()
        infra = _make_infra("bm0")

        observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

        payload = _decode_write(mock_run.call_args_list[1].args[0])
        charms = payload["features"]["observability"]["embedded"]["software"][
            "charms"
        ]
        assert charms["prometheus-k8s"]["storage"]["database"] == "40G"
        assert payload["core"] == {"config": {"microceph_config": {"osd": 1}}}

    @patch(
        "sunbeam_deployer.phases.observability._prompt_override_observability"
    )
    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_existing_segment_keep(
        self, mock_run: MagicMock, mock_prompt: MagicMock
    ) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, stdout=MANIFEST_WITH_OBSERVABILITY),
        ]
        mock_prompt.return_value = False
        cfg = _make_cfg()
        cfg.observability.storage.prometheus = "40G"
        mon = _make_mon()
        infra = _make_infra("bm0")

        observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

        # Only the initial cat is performed — no write/push.
        assert len(mock_run.call_args_list) == 1
        assert mock_run.call_args_list[0].args[0] == "cat /tmp/manifest.yaml"

    @patch(
        "sunbeam_deployer.phases.observability._prompt_override_observability"
    )
    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_no_existing_segment_no_prompt(
        self, mock_run: MagicMock, mock_prompt: MagicMock
    ) -> None:
        mock_run.side_effect = _run_host_side
        mock_prompt.return_value = True
        cfg = _make_cfg()
        mon = _make_mon()
        infra = _make_infra("bm0")

        observability._configure_manifest(cfg, mon, infra, infra.nodes[0])

        mock_prompt.assert_not_called()


# ---------------------------------------------------------------------------
# _prompt_override_observability
# ---------------------------------------------------------------------------


class TestPromptOverride:
    def test_yes(self) -> None:
        with patch("builtins.input", return_value="y"):
            assert observability._prompt_override_observability() is True

    def test_no(self) -> None:
        with patch("builtins.input", return_value="n"):
            assert observability._prompt_override_observability() is False

    def test_empty(self) -> None:
        with patch("builtins.input", return_value=""):
            assert observability._prompt_override_observability() is False

    def test_eof(self) -> None:
        def _raise(_prompt: str) -> str:
            raise EOFError

        with patch("builtins.input", side_effect=_raise):
            assert observability._prompt_override_observability() is False


# ---------------------------------------------------------------------------
# _enable_observability
# ---------------------------------------------------------------------------


class TestEnableObservability:
    @patch("sunbeam_deployer.phases.observability.run_in_vm")
    def test_command_and_timeout(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, stdout="")
        cfg = _make_cfg()
        mon = _make_mon()
        node = _make_node("bm0")

        observability._enable_observability(cfg, mon, node)

        assert mock_run.call_args.args[0] == "bm0"
        assert (
            mock_run.call_args.args[1]
            == "sunbeam enable --manifest ~/manifest.yaml "
            "observability embedded"
        )
        assert mock_run.call_args.kwargs["timeout"] == 5400

    @patch("sunbeam_deployer.phases.observability.run_in_vm")
    def test_failure_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, stdout="boom")
        cfg = _make_cfg()
        mon = _make_mon()

        with pytest.raises(
            RuntimeError, match="Failed to enable observability"
        ):
            observability._enable_observability(cfg, mon, _make_node("bm0"))


# ---------------------------------------------------------------------------
# _wait_cos_active
# ---------------------------------------------------------------------------


class TestWaitCosActive:
    @patch("sunbeam_deployer.phases.observability.run_in_vm")
    def test_all_active_passes(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=True, stdout=_juju_status(ALL_ACTIVE)
        )
        cfg = _make_cfg()
        mon = _make_mon()

        observability._wait_cos_active(cfg, mon, _make_node("bm0"))

    @patch("sunbeam_deployer.phases.observability.run_in_vm")
    def test_waiting_past_deadline_raises(self, mock_run: MagicMock) -> None:
        apps = dict(ALL_ACTIVE)
        apps["loki"] = "waiting"
        mock_run.return_value = MagicMock(ok=True, stdout=_juju_status(apps))
        cfg = _make_cfg()
        # A negative timeout makes the deadline already elapsed.
        cfg.timeouts.observability_verify = -1
        mon = _make_mon()

        with pytest.raises(RuntimeError, match="Timed out"):
            observability._wait_cos_active(cfg, mon, _make_node("bm0"))


class TestVerifyFeatures:
    @staticmethod
    def _run(verify_seconds: int) -> MagicMock:
        cfg = _make_cfg()
        cfg.timeouts.observability_verify = verify_seconds
        mon = _make_mon()
        with (
            patch(
                "sunbeam_deployer.phases.observability.push_file_to_vm",
                return_value=MagicMock(ok=True),
            ),
            patch(
                "sunbeam_deployer.phases.observability.run_in_vm",
                return_value=MagicMock(ok=True, stdout="PASS x\n"),
            ) as mock_run,
        ):
            observability._verify_features(cfg, mon, _make_node("bm0"))
        return mock_run

    def test_script_timeout_derived_from_config(self) -> None:
        """The script gets the step timeout minus a fixed headroom."""
        mock_run = self._run(1800)
        assert "--timeout 1680" in mock_run.call_args.args[1]
        assert mock_run.call_args.kwargs["timeout"] == 1800

    def test_script_timeout_has_a_floor(self) -> None:
        mock_run = self._run(100)
        assert "--timeout 60" in mock_run.call_args.args[1]


# ---------------------------------------------------------------------------
# run_phase
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_disabled_skips_without_add_phase(self) -> None:
        cfg = load_config(None)
        cfg.observability.enabled = False
        mon = DeploymentMonitor()
        infra = _make_infra("bm0", "bm1")

        observability.run_phase(cfg, mon, infra)

        assert mon.phases == []

    @patch("sunbeam_deployer.phases.observability.push_file_to_vm")
    @patch("sunbeam_deployer.phases.observability.run_in_vm")
    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_happy_path_success(
        self,
        mock_run_host: MagicMock,
        mock_run_in_vm: MagicMock,
        mock_push: MagicMock,
    ) -> None:
        mock_run_host.side_effect = _run_host_side
        mock_push.return_value = MagicMock(ok=True, stdout="")

        def _vm_side(vm: str, cmd: str, **kwargs: object) -> MagicMock:
            if "juju status" in cmd:
                return MagicMock(ok=True, stdout=_juju_status(ALL_ACTIVE))
            if "dashboard-url" in cmd:
                return MagicMock(
                    ok=True, stdout="http://10.0.0.1/observability-grafana"
                )
            if "verify_observability.py" in cmd:
                return MagicMock(ok=True, stdout="PASS grafana-health\n")
            return MagicMock(ok=True, stdout="")

        mock_run_in_vm.side_effect = _vm_side

        cfg = _make_cfg()
        mon = DeploymentMonitor()
        infra = _make_infra("bm0")

        observability.run_phase(cfg, mon, infra)

        assert mon.phases[0].name == observability.PHASE
        assert mon.phases[0].status == Status.SUCCESS

    @patch("sunbeam_deployer.phases.observability.run_host")
    def test_no_nodes_raises(self, mock_run_host: MagicMock) -> None:
        cfg = _make_cfg()
        mon = DeploymentMonitor()
        infra = _make_infra()

        with pytest.raises(RuntimeError, match="No compute nodes"):
            observability.run_phase(cfg, mon, infra)
