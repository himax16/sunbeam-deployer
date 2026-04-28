"""Tests for sunbeam_deployer.phases.cluster."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunbeam_deployer.config import load_config
from sunbeam_deployer.monitor import DeploymentMonitor
from sunbeam_deployer.phases.cluster import (
    _extract_token,
    _resolve_cluster_nodes,
    run_phase,
)
from sunbeam_deployer.phases.host_setup import ComputeNode, InfraInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(name: str) -> ComputeNode:
    return ComputeNode(
        name=name,
        fqdn=f"{name}.test.local",
        hostname=name,
        ip=f"10.0.0.{ord(name[-1])}",
        roles=["control", "compute"],
    )


def _make_infra(*names: str) -> InfraInfo:
    return InfraInfo(
        nodes=[_make_node(n) for n in names],
        plan_dir="/tmp/plan",
        manifest_path="/tmp/manifest.yaml",
        ssh_private_key_path="/tmp/key",
    )


def _make_cfg(cluster_node_count: int = 0):
    cfg = load_config(None)
    cfg.sunbeam.cluster_node_count = cluster_node_count
    return cfg


# ---------------------------------------------------------------------------
# _resolve_cluster_nodes
# ---------------------------------------------------------------------------


class TestResolveClusterNodes:
    def test_zero_returns_all_nodes(self) -> None:
        cfg = _make_cfg(cluster_node_count=0)
        infra = _make_infra("bm0", "bm1", "bm2")
        result = _resolve_cluster_nodes(cfg, infra)
        assert [n.name for n in result] == [
            "bm0",
            "bm1",
            "bm2",
        ]

    def test_count_one_returns_first_node(self) -> None:
        cfg = _make_cfg(cluster_node_count=1)
        infra = _make_infra("bm0", "bm1", "bm2")
        result = _resolve_cluster_nodes(cfg, infra)
        assert [n.name for n in result] == ["bm0"]

    def test_count_two_returns_first_two(self) -> None:
        cfg = _make_cfg(cluster_node_count=2)
        infra = _make_infra("bm0", "bm1", "bm2")
        result = _resolve_cluster_nodes(cfg, infra)
        assert [n.name for n in result] == ["bm0", "bm1"]

    def test_count_exceeding_total_returns_all(self) -> None:
        cfg = _make_cfg(cluster_node_count=10)
        infra = _make_infra("bm0", "bm1")
        result = _resolve_cluster_nodes(cfg, infra)
        assert [n.name for n in result] == ["bm0", "bm1"]

    def test_count_equal_to_total_returns_all(self) -> None:
        cfg = _make_cfg(cluster_node_count=3)
        infra = _make_infra("bm0", "bm1", "bm2")
        result = _resolve_cluster_nodes(cfg, infra)
        assert [n.name for n in result] == [
            "bm0",
            "bm1",
            "bm2",
        ]


# ---------------------------------------------------------------------------
# run_phase — single-node
# ---------------------------------------------------------------------------


class TestRunPhaseSingleNode:
    @patch("sunbeam_deployer.phases.cluster.run_in_vm")
    def test_single_node_skips_dns_and_join(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, stdout="")
        cfg = _make_cfg(cluster_node_count=1)
        mon = DeploymentMonitor()
        infra = _make_infra("bm0", "bm1", "bm2")

        run_phase(cfg, mon, infra)

        cmds = [call.args[1] for call in mock_run.call_args_list]
        assert any("bootstrap" in c for c in cmds)
        assert not any("cluster add" in c for c in cmds)
        assert not any("cluster join" in c for c in cmds)
        assert not any("getent" in c for c in cmds)


# ---------------------------------------------------------------------------
# run_phase — multi-node
# ---------------------------------------------------------------------------


class TestRunPhaseMultiNode:
    @patch("sunbeam_deployer.phases.cluster.run_in_vm")
    def test_all_nodes_default(self, mock_run: MagicMock) -> None:
        """With count=0, all nodes bootstrap + join."""
        token = "eyJ0ZXN0IjoiYWJjZGVmMTIzNDU2Nzg5MCJ9"
        mock_run.return_value = MagicMock(ok=True, stdout=f"token: {token}\n")
        cfg = _make_cfg(cluster_node_count=0)
        mon = DeploymentMonitor()
        infra = _make_infra("bm0", "bm1", "bm2")

        run_phase(cfg, mon, infra)

        cmds = [call.args[1] for call in mock_run.call_args_list]
        assert any("getent" in c for c in cmds)
        assert any("bootstrap" in c for c in cmds)
        assert sum("cluster add" in c for c in cmds) == 2
        assert sum("cluster join" in c for c in cmds) == 2

    @patch("sunbeam_deployer.phases.cluster.run_in_vm")
    def test_subset_join(self, mock_run: MagicMock) -> None:
        """With count=2, only first 2 nodes cluster."""
        token = "eyJ0ZXN0IjoiYWJjZGVmMTIzNDU2Nzg5MCJ9"
        mock_run.return_value = MagicMock(ok=True, stdout=f"token: {token}\n")
        cfg = _make_cfg(cluster_node_count=2)
        mon = DeploymentMonitor()
        infra = _make_infra("bm0", "bm1", "bm2")

        run_phase(cfg, mon, infra)

        cmds = [call.args[1] for call in mock_run.call_args_list]
        assert any("bootstrap" in c for c in cmds)
        assert sum("cluster add" in c for c in cmds) == 1
        assert sum("cluster join" in c for c in cmds) == 1


# ---------------------------------------------------------------------------
# run_phase — error cases
# ---------------------------------------------------------------------------


class TestRunPhaseErrors:
    def test_no_nodes_raises(self) -> None:
        cfg = _make_cfg()
        mon = DeploymentMonitor()
        infra = _make_infra()

        with pytest.raises(RuntimeError, match="No compute nodes"):
            run_phase(cfg, mon, infra)


class TestExtractToken:
    def test_yaml_format(self) -> None:
        output = "token: eyJhbGciOiJIUzI1NiJ9.dGVzdA.abc123\n"
        assert _extract_token(output) == "eyJhbGciOiJIUzI1NiJ9.dGVzdA.abc123"

    def test_yaml_format_with_quotes(self) -> None:
        output = "token: 'mytoken12345678901234'\n"
        assert _extract_token(output) == "mytoken12345678901234"

    def test_yaml_format_double_quotes(self) -> None:
        output = 'token: "mytoken12345678901234"\n'
        assert _extract_token(output) == "mytoken12345678901234"

    def test_yaml_format_with_prefix(self) -> None:
        output = (
            "Some preamble\n"
            "token: ABCDEFGHIJKLMNOPQRSTuvwxyz1234567890\n"
            "More text\n"
        )
        assert _extract_token(output) == "ABCDEFGHIJKLMNOPQRSTuvwxyz1234567890"

    def test_bare_base64_fallback(self) -> None:
        output = "Generating join token...\nABCDEFghijklmnop1234567890+/=\n"
        assert _extract_token(output) == "ABCDEFghijklmnop1234567890+/="

    def test_bare_base64_picks_last_line(self) -> None:
        output = (
            "short\n"
            "notavalidtokenxxxxxxxxxxxxxx\n"
            "ABCDEFGhijklmnopqrstuVWXYZ0123456789_.-\n"
        )
        result = _extract_token(output)
        assert result == "ABCDEFGhijklmnopqrstuVWXYZ0123456789_.-"

    def test_returns_none_for_empty(self) -> None:
        assert _extract_token("") is None

    def test_returns_none_for_no_token(self) -> None:
        output = "Some random output\nwithout any token\n"
        assert _extract_token(output) is None

    def test_returns_none_for_short_base64(self) -> None:
        # Strings < 20 chars should not be considered tokens
        output = "abc123\n"
        assert _extract_token(output) is None

    def test_yaml_empty_value(self) -> None:
        output = "token:\n"
        assert _extract_token(output) is None

    def test_real_world_multiline(self) -> None:
        token_val = (
            "eyJuYW1lIjoiYm0xLnJlcyIsInNlY3JldCI6ImFiY2RlZjEyMzQ1Njc4OTAi"
        )
        output = (
            f"Adding node bm1.res to cluster...\ntoken: {token_val}\nDone.\n"
        )
        token = _extract_token(output)
        assert token == token_val
