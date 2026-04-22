"""Tests for sunbeam_deployer.monitor — lifecycle, formatting, summary."""

from __future__ import annotations

import pytest

from sunbeam_deployer.monitor import (
    DeploymentMonitor,
    Phase,
    Status,
    Step,
)

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class TestStatus:
    def test_str_values(self) -> None:
        assert str(Status.PENDING) == "pending"
        assert str(Status.SUCCESS) == "success"
        assert str(Status.FAILED) == "failed"

    def test_symbols(self) -> None:
        assert Status.SUCCESS.symbol == "✅"
        assert Status.FAILED.symbol == "❌"
        assert Status.PENDING.symbol == "⏳"
        assert Status.RUNNING.symbol == "🔄"
        assert Status.SKIPPED.symbol == "⏭️"


# ---------------------------------------------------------------------------
# Step.duration_str
# ---------------------------------------------------------------------------


class TestStepDuration:
    def test_no_start_returns_dash(self) -> None:
        step = Step(name="test")
        assert step.duration_str == "-"

    def test_seconds_format(self) -> None:
        step = Step(name="test", start_time=100.0, end_time=130.0)
        assert step.duration_str == "30s"

    def test_minutes_format(self) -> None:
        step = Step(name="test", start_time=100.0, end_time=225.0)
        assert step.duration_str == "2m05s"

    def test_hours_format(self) -> None:
        step = Step(name="test", start_time=100.0, end_time=3800.0)
        assert step.duration_str == "1h01m40s"

    def test_duration_none_when_no_start(self) -> None:
        step = Step(name="test")
        assert step.duration is None


# ---------------------------------------------------------------------------
# Phase.duration_str
# ---------------------------------------------------------------------------


class TestPhaseDuration:
    def test_no_start_returns_dash(self) -> None:
        phase = Phase(name="test")
        assert phase.duration_str == "-"

    def test_with_start_and_end(self) -> None:
        phase = Phase(name="test", start_time=100.0, end_time=400.0)
        assert phase.duration_str == "5m00s"


# ---------------------------------------------------------------------------
# DeploymentMonitor — phase lifecycle
# ---------------------------------------------------------------------------


class TestDeploymentMonitorPhases:
    def test_add_phase(self) -> None:
        mon = DeploymentMonitor()
        phase = mon.add_phase("test-phase")
        assert phase.name == "test-phase"
        assert phase.status == Status.PENDING
        assert len(mon.phases) == 1

    def test_start_phase(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        phase = mon.start_phase("p")
        assert phase.status == Status.RUNNING
        assert phase.start_time is not None

    def test_end_phase_success(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.end_phase("p", Status.SUCCESS)
        assert mon.phases[0].status == Status.SUCCESS
        assert mon.phases[0].end_time is not None

    def test_end_phase_failed(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.end_phase("p", Status.FAILED, error="boom")
        assert mon.phases[0].status == Status.FAILED

    def test_multiple_phases(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("a")
        mon.add_phase("b")
        assert len(mon.phases) == 2
        assert mon.phases[0].name == "a"
        assert mon.phases[1].name == "b"


# ---------------------------------------------------------------------------
# DeploymentMonitor — step lifecycle
# ---------------------------------------------------------------------------


class TestDeploymentMonitorSteps:
    def test_add_step(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        step = mon.add_step("p", "s", "description")
        assert step.name == "s"
        assert step.description == "description"

    def test_start_step(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.add_step("p", "s")
        step = mon.start_step("p", "s")
        assert step.status == Status.RUNNING

    def test_end_step(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.add_step("p", "s")
        mon.start_step("p", "s")
        mon.end_step("p", "s", Status.SUCCESS)
        step = mon._find_step("p", "s")
        assert step.status == Status.SUCCESS

    def test_end_step_with_error(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.add_step("p", "s")
        mon.start_step("p", "s")
        mon.end_step("p", "s", Status.FAILED, error="oops")
        step = mon._find_step("p", "s")
        assert step.error == "oops"

    def test_find_step_raises_on_missing(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        with pytest.raises(KeyError, match="not found"):
            mon._find_step("p", "nonexistent")


# ---------------------------------------------------------------------------
# run_step context manager
# ---------------------------------------------------------------------------


class TestStepContext:
    def test_success_on_normal_exit(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        with mon.run_step("p", "s", "desc"):
            pass
        step = mon._find_step("p", "s")
        assert step.status == Status.SUCCESS

    def test_failure_on_exception(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        with pytest.raises(ValueError), mon.run_step("p", "s", "desc"):
            raise ValueError("boom")
        step = mon._find_step("p", "s")
        assert step.status == Status.FAILED
        assert step.error == "boom"

    def test_does_not_suppress_exception(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        with pytest.raises(RuntimeError), mon.run_step("p", "s"):
            raise RuntimeError("propagate me")


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_contains_phase_names(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("alpha")
        mon.start_phase("alpha")
        mon.end_phase("alpha", Status.SUCCESS)
        text = mon.summary()
        assert "alpha" in text

    def test_summary_shows_success(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.end_phase("p", Status.SUCCESS)
        text = mon.summary()
        assert "SUCCESS" in text
        assert "✅" in text

    def test_summary_shows_failure(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.end_phase("p", Status.FAILED)
        text = mon.summary()
        assert "FAILED" in text

    def test_summary_includes_steps(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        with mon.run_step("p", "my-step", "My step description"):
            pass
        mon.end_phase("p", Status.SUCCESS)
        text = mon.summary()
        assert "My step description" in text

    def test_summary_includes_step_errors(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.add_step("p", "s", "failing step")
        mon.start_step("p", "s")
        mon.end_step("p", "s", Status.FAILED, error="something broke")
        mon.end_phase("p", Status.FAILED)
        text = mon.summary()
        assert "something broke" in text

    def test_summary_overall_failed_if_any_phase_failed(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("a")
        mon.start_phase("a")
        mon.end_phase("a", Status.SUCCESS)
        mon.add_phase("b")
        mon.start_phase("b")
        mon.end_phase("b", Status.FAILED)
        text = mon.summary()
        assert "FAILED" in text

    def test_summary_skipped_counts_as_success(self) -> None:
        mon = DeploymentMonitor()
        mon.add_phase("p")
        mon.start_phase("p")
        mon.end_phase("p", Status.SKIPPED)
        text = mon.summary()
        assert "SUCCESS" in text

    def test_summary_contains_header(self) -> None:
        mon = DeploymentMonitor()
        text = mon.summary()
        assert "Sunbeam Deployment Summary" in text
        assert "Total time:" in text
