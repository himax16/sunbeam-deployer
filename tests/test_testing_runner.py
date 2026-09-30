"""Tests for sunbeam_deployer.testing.runner (no browser, no network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunbeam_deployer.testing import runner

_ACTION_OUT = (
    "Running operation 7 with 1 task\n"
    "  - task 8 on unit-grafana-0\n\n"
    "Waiting for task 8...\n"
    '{"grafana/0":{"id":"8","results":{"admin-password":"s3cret",'
    '"return-code":0,"url":"http://192.167.98.237/observability-grafana"},'
    '"status":"completed","unit":"grafana/0"}}\n'
)


class TestGrafanaCredentials:
    @patch("sunbeam_deployer.testing.runner.run_in_vm")
    def test_parses_after_progress_lines(self, mock_vm: MagicMock) -> None:
        mock_vm.return_value = MagicMock(ok=True, stdout=_ACTION_OUT)
        url, password = runner.grafana_credentials("bm0")
        assert url == "http://192.167.98.237/observability-grafana"
        assert password == "s3cret"
        assert mock_vm.call_args.kwargs["stream"] is False

    @patch("sunbeam_deployer.testing.runner.run_in_vm")
    def test_action_failure_raises(self, mock_vm: MagicMock) -> None:
        mock_vm.return_value = MagicMock(ok=False, stdout="boom")
        with pytest.raises(RuntimeError, match="admin password"):
            runner.grafana_credentials("bm0")

    @patch("sunbeam_deployer.testing.runner.run_in_vm")
    def test_bad_output_raises(self, mock_vm: MagicMock) -> None:
        mock_vm.return_value = MagicMock(ok=True, stdout="not json")
        with pytest.raises(RuntimeError, match="Unexpected"):
            runner.grafana_credentials("bm0")


class TestReachableUrl:
    @patch("sunbeam_deployer.testing.runner.get_remote_target")
    def test_local_passthrough(self, mock_target: MagicMock) -> None:
        mock_target.return_value = None
        with runner.reachable_url("http://1.2.3.4/grafana") as url:
            assert url == "http://1.2.3.4/grafana"


_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="t" name="test_login[chromium]" />
<testcase classname="t" name="test_listed[chromium-Capacity]">
  <failure message="boom">trace</failure>
</testcase>
<testcase classname="t" name="test_err"><error message="x" /></testcase>
<testcase classname="t" name="test_skip"><skipped message="s" /></testcase>
</testsuite></testsuites>
"""


class TestParseJunitReport:
    def test_counts_outcomes(self, tmp_path) -> None:
        path = tmp_path / "report.xml"
        path.write_text(_JUNIT)
        report = runner.parse_junit_report(path)
        assert report.total == 4
        assert report.passed == ["test_login[chromium]"]
        assert report.failed == [
            "test_listed[chromium-Capacity]",
            "test_err",
        ]
        assert report.skipped == ["test_skip"]
        assert report.summary == "1 passed, 2 failed, 1 skipped (of 4)"

    def test_missing_report_is_logged_not_raised(self, tmp_path) -> None:
        runner._log_report(tmp_path / "absent.xml")


class TestDefaultArtifactsDir:
    def test_timestamped_under_screenshots(self) -> None:
        path = runner.default_artifacts_dir()
        assert path.parent.name == "screenshots"
        assert "~" not in str(path)
