"""Phase/step status tracking and deployment summary."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from rich.table import Table
from rich.text import Text

import sunbeam_deployer.logger as logger

log = logging.getLogger("sunbeam_deployer.monitor")

SUMMARY_PHASE_COL_WIDTH = 60
SUMMARY_STATUS_COL_WIDTH = 8
SUMMARY_DURATION_COL_WIDTH = 10


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


class SingleLineText(Text):
    """Helper class to render a single line text in rich Table"""

    def __rich_measure__(self, console, options):
        return (
            super()
            .__rich_measure__(console, options)
            .with_minimum(options.max_width)
        )


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
        logger.set_log_indent(0)
        logger.update_spinner(f"Phase: {name}")
        log.info("━━━ Phase: %s ━━━", name)
        logger.set_log_indent(1)
        return phase

    def end_phase(
        self, name: str, status: Status, error: str | None = None
    ) -> None:
        phase = self._phase_map[name]
        phase.status = status
        phase.end_time = time.monotonic()
        logger.set_log_indent(0)
        sym = status.symbol
        log.info(
            "%s Phase '%s' %s (%s)", sym, name, status.value, phase.duration_str
        )

    # -- Step lifecycle --------------------------------------------------------

    def add_step(
        self, phase_name: str, step_name: str, description: str = ""
    ) -> Step:
        step = Step(name=step_name, description=description)
        self._phase_map[phase_name].steps.append(step)
        return step

    def start_step(self, phase_name: str, step_name: str) -> Step:
        step = self._find_step(phase_name, step_name)
        step.status = Status.RUNNING
        logger.set_log_indent(1)
        desc = step.description or step_name
        logger.update_spinner(f"Phase: {phase_name} — {desc}")
        log.info("▸ %s", desc)
        logger.set_log_indent(2)
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
        logger.set_log_indent(1)
        if status == Status.FAILED:
            log.error(
                "%s %s — FAILED (%s)",
                status.symbol,
                step_name,
                step.duration_str,
            )
            if error:
                logger.set_log_indent(2)
                for line in _extract_error_lines(error):
                    log.error("%s", line)
                logger.set_log_indent(1)
        else:
            log.debug("%s %s (%s)", status.symbol, step_name, step.duration_str)

    def run_step(
        self,
        phase_name: str,
        step_name: str,
        description: str = "",
    ):
        """Context-manager that tracks a step's lifecycle."""
        return _StepContext(self, phase_name, step_name, description)

    # -- Summary ---------------------------------------------------------------

    def summary(self) -> Table:
        """Return a rich Table summarising the deployment."""
        total = time.monotonic() - self._overall_start
        mins, secs = divmod(int(total), 60)
        hours, mins = divmod(mins, 60)
        time_str = (
            f"{hours}h{mins:02d}m{secs:02d}s"
            if hours
            else f"{mins}m{secs:02d}s"
        )

        overall = (
            Status.SUCCESS
            if all(
                p.status in (Status.SUCCESS, Status.SKIPPED)
                for p in self.phases
            )
            else Status.FAILED
        )
        overall_style = "green" if overall == Status.SUCCESS else "red"

        table = Table(
            title="Sunbeam Deployment Summary",
            title_style="bold",
            header_style="bold cyan",
            border_style="bright_blue",
            caption_justify="right",
            expand=True,
        )
        table.add_column(
            "Phase / Step",
            style="bold",
        )
        table.add_column(
            "Status",
            justify="center",
            width=SUMMARY_STATUS_COL_WIDTH,
        )
        table.add_column(
            "Duration",
            justify="right",
            width=SUMMARY_DURATION_COL_WIDTH,
        )

        for phase in self.phases:
            pstyle = (
                "green"
                if phase.status == Status.SUCCESS
                else "red"
                if phase.status == Status.FAILED
                else "yellow"
            )
            table.add_row(
                phase.name,
                f"[{pstyle}]{phase.status.symbol}[/]",
                phase.duration_str,
            )
            for step in phase.steps:
                sstyle = (
                    "green"
                    if step.status == Status.SUCCESS
                    else "red"
                    if step.status == Status.FAILED
                    else "yellow"
                )
                desc = step.description or step.name
                table.add_row(
                    f"  {desc}",
                    f"[{sstyle}]{step.status.symbol}[/]",
                    step.duration_str,
                )
                if step.error:
                    err_lines = _extract_error_lines(step.error)
                    for err_line in err_lines:
                        err_log = SingleLineText(
                            f"    {err_line}",
                            no_wrap=True,
                            overflow="ellipsis",
                        )
                        err_log.stylize("red")
                        table.add_row(err_log, "", "")

            # If there is a next phase, add a separator row
            if phase != self.phases[-1]:
                table.add_section()

        # Add footer with where the log file is located and the total time taken
        footer = (
            f"Log file: {logger._log_file_path}\n"
            f"Total time: {time_str}   "
            f"Overall: [{overall_style}]{overall.symbol} "
            f"{overall.value.upper()}[/]"
        )
        table.caption = footer

        return table

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


def _extract_error_lines(error: str) -> list[str]:
    """Extract meaningful error/diagnostic lines from a command's stdout.

    Filters out Terraform plan details (lines with ``"network"``, ``device``,
    ``properties``, etc.) and keeps only actionable lines: Terraform ``Error:``
    blocks, ``Traceback``, ``RuntimeError``, ``exit code``, and the last few
    lines of the output.
    """
    lines = error.splitlines()
    # Keep lines that are clearly diagnostic
    keep: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Always keep these
        if any(
            kw in stripped
            for kw in (
                "Error:",
                "Traceback",
                "RuntimeError",
                "exit code",
                "Command failed",
                "already exists",
                "FAILED",
                "failed after",
            )
        ):
            keep.append(line)
    # If nothing matched, keep last 3 lines as a fallback
    if not keep:
        keep = lines[-3:]
    return keep[-5:]  # max 5 lines
