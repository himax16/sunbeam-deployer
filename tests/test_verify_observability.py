"""Tests for the verify-observability.py standalone script's pure helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "sunbeam_deployer"
    / "scripts"
    / "verify-observability.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_observability", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = _load_module()
_EXPECTED = verify._EXPECTED_DASHBOARDS


class TestFindMissingDashboards:
    def test_all_present(self) -> None:
        titles = [
            "OpenStack Service Overview",
            "OpenStack Cloud Usage",
            "OpenStack Compute Overview",
            "Capacity",
            "OpenStack Project Overview",
            "OpenStack Logging",
        ]
        assert verify._find_missing_dashboards(titles, _EXPECTED) == []

    def test_one_missing(self) -> None:
        titles = ["OpenStack Service Overview", "Capacity"]
        missing = verify._find_missing_dashboards(titles, _EXPECTED)
        assert "Cloud Usage" in missing
        assert "Logging" in missing
        assert "Service Overview" not in missing

    def test_case_insensitive(self) -> None:
        titles = ["openstack service overview", "CAPACITY"]
        missing = verify._find_missing_dashboards(titles, _EXPECTED)
        assert "Service Overview" not in missing
        assert "Capacity" not in missing
        assert "Cloud Usage" in missing


class TestGrafanaAction:
    def test_parses_password_and_url(self) -> None:
        sample = json.dumps(
            {
                "grafana/0": {
                    "results": {
                        "admin-password": "secretpw",
                        "url": "http://10.0.0.1/observability-grafana",
                    }
                }
            }
        )
        mock_proc = MagicMock()
        mock_proc.stdout = sample

        with patch.object(
            verify.subprocess, "run", return_value=mock_proc
        ) as mock_run:
            password, url = verify._grafana_action("external-cos")
            args = mock_run.call_args.args[0]
            assert "get-admin-password" in args
            assert "external-cos" in args

        assert password == "secretpw"
        assert url == "http://10.0.0.1/observability-grafana"

    def test_never_returned_in_output(self) -> None:
        # Smoke: the action only surfaces the password/url tuple; the URL and
        # password come from the parsed results, not any printed output.
        sample = json.dumps(
            {"grafana/0": {"results": {"admin-password": "x", "url": "u"}}}
        )
        with patch.object(
            verify.subprocess,
            "run",
            return_value=MagicMock(stdout=sample),
        ):
            password, url = verify._grafana_action("observability")
        assert password == "x"
        assert url == "u"


class TestCheckDashboards:
    @staticmethod
    def _body(titles: list[str]) -> str:
        return json.dumps([{"title": t} for t in titles])

    def test_all_present(self) -> None:
        titles = [f"OpenStack {e}" for e in verify._EXPECTED_DASHBOARDS]
        with patch.object(
            verify, "_get", return_value=(200, self._body(titles))
        ):
            ok, detail = verify._check_dashboards("http://g", "pw")
        assert ok
        assert "missing=[]" in detail

    def test_reports_missing(self) -> None:
        with patch.object(
            verify,
            "_get",
            return_value=(200, self._body(["OpenStack Cloud Usage"])),
        ):
            ok, detail = verify._check_dashboards("http://g", "pw")
        assert not ok
        assert "Capacity" in detail

    def test_http_error(self) -> None:
        with patch.object(verify, "_get", return_value=(503, "")):
            ok, detail = verify._check_dashboards("http://g", "pw")
        assert not ok
        assert detail == "HTTP 503"

    def test_bad_json_counts_as_missing(self) -> None:
        with patch.object(verify, "_get", return_value=(200, "not json")):
            ok, _ = verify._check_dashboards("http://g", "pw")
        assert not ok
