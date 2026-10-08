"""Tests for sunbeam_deployer.phases.external_observability."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunbeam_deployer.config import load_config
from sunbeam_deployer.monitor import DeploymentMonitor
from sunbeam_deployer.phases import external_observability as ext
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo


def _make_node(name: str) -> ComputeNode:
    return ComputeNode(
        name=name,
        fqdn=f"{name}.test.local",
        hostname=name,
        ip="10.0.0.10",
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
    cfg.observability.external.enabled = True
    return cfg


def _make_mon() -> DeploymentMonitor:
    mon = DeploymentMonitor()
    mon.add_phase(ext.PHASE)
    mon.start_phase(ext.PHASE)
    return mon


_OFFER_URLS = ("u.grafana", "u.prometheus", "u.loki")


# ---------------------------------------------------------------------------
# ExternalObservabilityConfig
# ---------------------------------------------------------------------------


class TestExternalConfig:
    def test_defaults(self) -> None:
        e = load_config(None).observability.external
        assert e.enabled is False
        assert e.controller is None
        assert e.model == "external-cos"
        assert e.channel == "latest/stable"

    def test_validate_empty_model(self) -> None:
        cfg = load_config(None)
        cfg.observability.external.enabled = True
        cfg.observability.external.model = ""
        assert any("model" in err for err in cfg.validate())


# ---------------------------------------------------------------------------
# _is_separate / _controller_name
# ---------------------------------------------------------------------------


class TestControllerMode:
    def test_is_separate_none_is_false(self) -> None:
        cfg = _make_cfg()  # controller defaults to None
        assert ext._is_separate(cfg.observability.external) is False

    def test_is_separate_named_is_true(self) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        assert ext._is_separate(cfg.observability.external) is True

    def test_controller_name_defaults_to_sunbeam_controller(self) -> None:
        cfg = _make_cfg()
        assert ext._controller_name(cfg.observability.external) == (
            "sunbeam-controller"
        )

    def test_controller_name_uses_named(self) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        assert ext._controller_name(cfg.observability.external) == (
            "cos-controller"
        )


# ---------------------------------------------------------------------------
# _parse_json / _parse_offer_url
# ---------------------------------------------------------------------------


class TestParseJson:
    def test_parses_with_trailing_stderr_note(self) -> None:
        out = (
            '{"square-deer-k8s":{"type":"k8s"}}\n'
            "Only clouds with registered credentials are shown.\n"
        )
        assert ext._parse_json(out) == {"square-deer-k8s": {"type": "k8s"}}

    def test_empty_on_garbage(self) -> None:
        assert ext._parse_json("not json at all") == {}


class TestParseOfferUrl:
    def test_extracts_url(self) -> None:
        out = (
            'Application "grafana" endpoints [grafana-dashboard] '
            'available at "admin/external-cos.grafana"'
        )
        assert ext._parse_offer_url(out) == "admin/external-cos.grafana"

    def test_none_when_absent(self) -> None:
        assert ext._parse_offer_url("no url here") is None


# ---------------------------------------------------------------------------
# _bootstrap_controller / _controller_exists
# ---------------------------------------------------------------------------


class TestControllerExists:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_true_when_present(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True,
            stdout=json.dumps({"controllers": {"cos-controller": {}}}),
        )
        assert ext._controller_exists(_make_node("bm0"), "cos-controller") is (
            True
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_false_when_absent(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True, stdout=json.dumps({"controllers": {}})
        )
        assert ext._controller_exists(_make_node("bm0"), "cos-controller") is (
            False
        )


class TestBootstrapController:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_skips_when_exists(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        mon = _make_mon()
        with patch.object(
            ext, "_controller_exists", return_value=True
        ) as mock_exists:
            ext._bootstrap_controller(cfg, mon, _make_node("bm0"))

        assert mock_exists.call_count == 1
        assert run_in_vm.call_count == 0

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_bootstraps_with_k8s_cloud(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")
        with (
            patch.object(ext, "_controller_exists", return_value=False),
            patch.object(ext, "_k8s_cloud", return_value="square-deer-k8s"),
        ):
            ext._bootstrap_controller(cfg, mon, _make_node("bm0"))

        assert run_in_vm.call_args.args[1] == (
            "juju bootstrap square-deer-k8s cos-controller"
        )


# ---------------------------------------------------------------------------
# _k8s_cloud
# ---------------------------------------------------------------------------


class TestK8sCloud:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_finds_k8s_type_cloud(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True,
            stdout=json.dumps(
                {
                    "square-deer": {"type": "manual"},
                    "square-deer-k8s": {"type": "k8s"},
                }
            ),
        )
        assert ext._k8s_cloud(_make_node("bm0")) == "square-deer-k8s"

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_no_k8s_cloud_raises(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True, stdout=json.dumps({"lxd": {"type": "lxd"}})
        )
        with pytest.raises(RuntimeError):
            ext._k8s_cloud(_make_node("bm0"))


# ---------------------------------------------------------------------------
# _deploy_cos_stack
# ---------------------------------------------------------------------------


class TestDeployCosStack:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_same_controller_uses_k8s_cloud(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()  # controller None -> same controller
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")

        with (
            patch.object(ext, "_model_exists", return_value=False),
            patch.object(ext, "_k8s_cloud", return_value="square-deer-k8s"),
            patch.object(ext, "_wait_cos_active"),
        ):
            ext._deploy_cos_stack(cfg, mon, _make_node("bm0"))

        cmds = [c.args[1] for c in run_in_vm.call_args_list]
        assert "juju add-model external-cos square-deer-k8s" in cmds
        assert any(
            "juju deploy -m external-cos cos-lite" in c and "--trust" in c
            for c in cmds
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_separate_controller_switches(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")

        with (
            patch.object(ext, "_model_exists", return_value=False),
            patch.object(ext, "_wait_cos_active"),
        ):
            ext._deploy_cos_stack(cfg, mon, _make_node("bm0"))

        cmds = [c.args[1] for c in run_in_vm.call_args_list]
        assert "juju switch cos-controller" in cmds
        assert "juju add-model external-cos" in cmds


# ---------------------------------------------------------------------------
# _create_offers
# ---------------------------------------------------------------------------


class TestCreateOffers:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_offers_and_returns_urls(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()  # same controller -> no switch
        mon = _make_mon()
        outputs = [
            'available at "bm0.res/external-cos.grafana"',
            'available at "bm0.res/external-cos.prometheus"',
            'available at "bm0.res/external-cos.loki"',
        ]
        run_in_vm.side_effect = [MagicMock(ok=True, stdout=o) for o in outputs]

        urls = ext._create_offers(cfg, mon, _make_node("bm0"))

        cmds = [c.args[1] for c in run_in_vm.call_args_list]
        assert "juju offer external-cos.grafana:grafana-dashboard" in cmds
        assert "juju offer external-cos.prometheus:receive-remote-write" in cmds
        assert "juju offer external-cos.loki:logging" in cmds
        assert urls == (
            "bm0.res/external-cos.grafana",
            "bm0.res/external-cos.prometheus",
            "bm0.res/external-cos.loki",
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_existing_offer_falls_back_to_owner(
        self, run_in_vm: MagicMock
    ) -> None:
        cfg = _make_cfg()
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(
            ok=False, stdout="ERROR application offer already exists"
        )

        with patch.object(ext, "_model_owner", return_value="bm0.res"):
            urls = ext._create_offers(cfg, mon, _make_node("bm0"))

        assert urls == (
            "bm0.res/external-cos.grafana",
            "bm0.res/external-cos.prometheus",
            "bm0.res/external-cos.loki",
        )


# ---------------------------------------------------------------------------
# _register_controller
# ---------------------------------------------------------------------------


class TestRegisterController:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_copies_account_for_same_controller(
        self, run_in_vm: MagicMock
    ) -> None:
        cfg = _make_cfg()  # controller None -> sunbeam-controller
        mon = _make_mon()
        run_in_vm.side_effect = [
            MagicMock(ok=False, stdout=""),
            MagicMock(ok=True, stdout=""),
        ]

        ext._register_controller(cfg, mon, _make_node("bm0"))

        cmd = run_in_vm.call_args_list[1].args[1]
        assert cmd == (
            "cp ~/snap/openstack/current/account.yaml "
            "~/snap/openstack/current/sunbeam-controller.yaml"
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_copies_account_for_named_controller(
        self, run_in_vm: MagicMock
    ) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        mon = _make_mon()
        run_in_vm.side_effect = [
            MagicMock(ok=False, stdout=""),
            MagicMock(ok=True, stdout=""),
        ]

        ext._register_controller(cfg, mon, _make_node("bm0"))

        cmd = run_in_vm.call_args_list[1].args[1]
        assert cmd == (
            "cp ~/snap/openstack/current/account.yaml "
            "~/snap/openstack/current/cos-controller.yaml"
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_skips_when_already_present(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")

        ext._register_controller(cfg, mon, _make_node("bm0"))

        assert run_in_vm.call_count == 1


# ---------------------------------------------------------------------------
# _enable_external_observability
# ---------------------------------------------------------------------------


class TestEnableExternalObservability:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_command_uses_sunbeam_controller_when_none(
        self, run_in_vm: MagicMock
    ) -> None:
        cfg = _make_cfg()
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")

        ext._enable_external_observability(
            cfg, mon, _make_node("bm0"), _OFFER_URLS
        )

        cmd = run_in_vm.call_args.args[1]
        assert cmd == (
            "sunbeam -v enable observability external sunbeam-controller "
            "u.grafana u.prometheus u.loki"
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_command_uses_named_controller(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        cfg.observability.external.controller = "cos-controller"
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=True, stdout="")

        ext._enable_external_observability(
            cfg, mon, _make_node("bm0"), _OFFER_URLS
        )

        cmd = run_in_vm.call_args.args[1]
        assert cmd == (
            "sunbeam -v enable observability external cos-controller "
            "u.grafana u.prometheus u.loki"
        )

    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_enable_error_raises(self, run_in_vm: MagicMock) -> None:
        cfg = _make_cfg()
        mon = _make_mon()
        run_in_vm.return_value = MagicMock(ok=False, stdout="boom")

        with pytest.raises(RuntimeError):
            ext._enable_external_observability(
                cfg, mon, _make_node("bm0"), _OFFER_URLS
            )


# ---------------------------------------------------------------------------
# _model_exists / _model_owner / _parse_grafana_url
# ---------------------------------------------------------------------------


class TestModelExists:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_true_when_present(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True,
            stdout=json.dumps({"models": [{"short-name": "external-cos"}]}),
        )
        assert ext._model_exists(_make_node("bm0"), "external-cos") is True


class TestModelOwner:
    @patch("sunbeam_deployer.phases.external_observability.run_in_vm")
    def test_returns_owner(self, run_in_vm: MagicMock) -> None:
        run_in_vm.return_value = MagicMock(
            ok=True,
            stdout=json.dumps(
                {
                    "models": [
                        {
                            "short-name": "external-cos",
                            "name": "bm0.res/external-cos",
                        }
                    ]
                }
            ),
        )
        assert ext._model_owner(_make_node("bm0"), "external-cos") == "bm0.res"


class TestParseGrafanaUrl:
    def test_extracts_url(self) -> None:
        result = MagicMock(
            stdout=(
                '{"grafana/0": {"results": '
                '{"admin-password": "p", "url": "http://x/external-cos-grafana"}}}'
            )
        )
        assert ext._parse_grafana_url(result) == (
            "http://x/external-cos-grafana"
        )

    def test_rejects_missing_url(self) -> None:
        result = MagicMock(
            stdout='{"grafana/0": {"results": {"admin-password": "p"}}}'
        )
        with pytest.raises(RuntimeError):
            ext._parse_grafana_url(result)


# ---------------------------------------------------------------------------
# run_phase
# ---------------------------------------------------------------------------


class TestRunPhase:
    def test_disabled_skips_without_add_phase(self) -> None:
        cfg = load_config(None)
        cfg.observability.external.enabled = False
        mon = DeploymentMonitor()

        ext.run_phase(cfg, mon, _make_infra("bm0"))

        assert ext.PHASE not in [p.name for p in mon.phases]
