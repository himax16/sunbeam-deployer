"""Phase/step status tracking and deployment summary."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("sunbeam_deployer.monitor")


class Status(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"

    def __str__(self) -> str:
        return self.value

    @property
    def symbol(self) -> str:
        return {
            Status.PENDING: "⏳",
            Status.RUNNING: "🔄",
            Status.SUCCESS: "✅",
            Status.FAILED: "❌",
            Status.SKIPPED: "⏭️",
        }[self]


@dataclass
class Step:
    name: str
    description: str = ""
    status: Status = Status.PENDING
    start_time: float | None = None
    end_time: float | None = None
    error: str | None = None

    @property
    def duration(self) -> float | None:
        if self.start_time is None:
            return None
        end = self.end_time or time.monotonic()
        return end - self.start_time

    @property
    def duration_str(self) -> str:
        d = self.duration
        if d is None:
            return "-"
        if d < 60:
            return f"{d:.0f}s"
        mins, secs = divmod(int(d), 60)
        if mins < 60:
            return f"{mins}m{secs:02d}s"
        hours, mins = divmod(mins, 60)
        return f"{hours}h{mins:02d}m{secs:02d}s"


@dataclass
class Phase:
    name: str
    steps: list[Step] = field(default_factory=list)
    status: Status = Status.PENDING
    start_time: float | None = None
    end_time: float | None = None

    @property
    def duration_str(self) -> str:
        if self.start_time is None:
            return "-"
        end = self.end_time or time.monotonic()
        d = end - self.start_time
        mins, secs = divmod(int(d), 60)
        if mins < 60:
            return f"{mins}m{secs:02d}s"
        hours, mins = divmod(mins, 60)
        return f"{hours}h{mins:02d}m{secs:02d}s"


class DeploymentMonitor:
    """Tracks overall deployment progress across phases and steps."""

    def __init__(self) -> None:
        self.phases: list[Phase] = []
        self._phase_map: dict[str, Phase] = {}
        self._overall_start: float = time.monotonic()

    # -- Phase lifecycle -------------------------------------------------------

    def add_phase(self, name: str) -> Phase:
        phase = Phase(name=name)
        self.phases.append(phase)
        self._phase_map[name] = phase
        return phase

    def start_phase(self, name: str) -> Phase:
        phase = self._phase_map[name]
        phase.status = Status.RUNNING
        phase.start_time = time.monotonic()
        log.info("━━━ Phase: %s ━━━", name)
        return phase

    def end_phase(self, name: str, status: Status, error: str | None = None) -> None:
        phase = self._phase_map[name]
        phase.status = status
        phase.end_time = time.monotonic()
        sym = status.symbol
        log.info("%s Phase '%s' %s (%s)", sym, name, status.value, phase.duration_str)

    # -- Step lifecycle --------------------------------------------------------

    def add_step(self, phase_name: str, step_name: str, description: str = "") -> Step:
        step = Step(name=step_name, description=description)
        self._phase_map[phase_name].steps.append(step)
        return step

    def start_step(self, phase_name: str, step_name: str) -> Step:
        step = self._find_step(phase_name, step_name)
        step.status = Status.RUNNING
        step.start_time = time.monotonic()
        log.info("  ▸ %s", step.description or step_name)
        return step

    def end_step(
        self,
        phase_name: str,
        step_name: str,
        status: Status,
        error: str | None = None,
    ) -> None:
        step = self._find_step(phase_name, step_name)
        step.status = status
        step.end_time = time.monotonic()
        step.error = error
        if status == Status.FAILED:
            log.error("  %s %s — FAILED (%s)", status.symbol, step_name, step.duration_str)
            if error:
                for line in error.splitlines()[:5]:
                    log.error("    %s", line)
        else:
            log.debug("  %s %s (%s)", status.symbol, step_name, step.duration_str)

    def run_step(
        self,
        phase_name: str,
        step_name: str,
        description: str = "",
    ):
        """Context-manager that tracks a step's lifecycle."""
        return _StepContext(self, phase_name, step_name, description)

    # -- Summary ---------------------------------------------------------------

    def summary(self) -> str:
        total = time.monotonic() - self._overall_start
        mins, secs = divmod(int(total), 60)
        hours, mins = divmod(mins, 60)
        time_str = f"{hours}h{mins:02d}m{secs:02d}s" if hours else f"{mins}m{secs:02d}s"

        lines = [
            "",
            "╔══════════════════════════════════════════════════════╗",
            "║            Sunbeam Deployment Summary                ║",
            "╚══════════════════════════════════════════════════════╝",
            f"  Total time: {time_str}",
            "",
        ]

        for phase in self.phases:
            lines.append(f"  {phase.status.symbol} {phase.name} ({phase.duration_str})")
            for step in phase.steps:
                lines.append(
                    f"      {step.status.symbol} {step.description or step.name} ({step.duration_str})"
                )
                if step.error:
                    for err_line in step.error.splitlines()[:3]:
                        lines.append(f"          ⚠ {err_line}")

        overall = Status.SUCCESS if all(
            p.status in (Status.SUCCESS, Status.SKIPPED) for p in self.phases
        ) else Status.FAILED
        lines.append("")
        lines.append(f"  Overall: {overall.symbol} {overall.value.upper()}")
        lines.append("")
        return "\n".join(lines)

    # -- Internals -------------------------------------------------------------

    def _find_step(self, phase_name: str, step_name: str) -> Step:
        phase = self._phase_map[phase_name]
        for step in phase.steps:
            if step.name == step_name:
                return step
        raise KeyError(f"Step '{step_name}' not found in phase '{phase_name}'")


class _StepContext:
    """Context manager for tracking step lifecycle."""

    def __init__(
        self, monitor: DeploymentMonitor, phase: str, step: str, desc: str
    ) -> None:
        self._monitor = monitor
        self._phase = phase
        self._step = step
        self._desc = desc

    def __enter__(self) -> Step:
        self._monitor.add_step(self._phase, self._step, self._desc)
        return self._monitor.start_step(self._phase, self._step)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self._monitor.end_step(self._phase, self._step, Status.SUCCESS)
        else:
            self._monitor.end_step(
                self._phase,
                self._step,
                Status.FAILED,
                error=str(exc_val),
            )
        return False  # don't suppress
