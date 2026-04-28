"""Subcommand handlers for CLI operations."""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sunbeam_deployer.executor import run_local
from sunbeam_deployer.phases import testflinger

log = logging.getLogger("sunbeam_deployer.commands")

# Regex to identify a job line starting with a UUID
_UUID_RE = re.compile(r"^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})")

# ``testflinger jobs --status`` line format:
#   UUID STATUS DAY MON DD HH:MM QUEUE
_LINE_STATUS_RE = re.compile(
    r"^([0-9a-f-]{36})\s+"
    r"(\S+)\s+"
    r"(\w{3}\s+\w{3}\s+\d+\s+\d+:\d+)\s+"
    r"(\S+)\s*$",
)

# ``testflinger jobs`` line format (no status column):
#   UUID DAY MON DD HH:MM QUEUE
_LINE_PLAIN_RE = re.compile(
    r"^([0-9a-f-]{36})\s+"
    r"(\w{3}\s+\w{3}\s+\d+\s+\d+:\d+)\s+"
    r"(\S+)\s*$",
)

_ACTIVE_PHASES = {
    "reserve",
    "test",
    "provision",
    "setup",
    "allocate",
    "waiting",
    "queued",
    "firmware_update",
}


@dataclass
class JobInfo:
    """Information about a Testflinger job."""

    job_id: str
    status: str | None = None
    device_ip: str | None = None
    agent_name: str | None = None
    submitted_at: str | None = None
    submitted_dt: datetime | None = field(default=None, repr=False)
    queue: str | None = None
    reserve_timeout: int | None = None

    def runtime_str(self) -> str:
        """Human-readable runtime since submission."""
        if not self.submitted_dt:
            return "N/A"
        delta = datetime.now(tz=timezone.utc) - self.submitted_dt
        total_secs = int(delta.total_seconds())
        if total_secs < 0:
            return "N/A"
        return _fmt_duration(total_secs)

    def reserve_timeout_str(self) -> str:
        """Human-readable reserve timeout."""
        if self.reserve_timeout is None:
            return "N/A"
        return _fmt_duration(self.reserve_timeout)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "device_ip": self.device_ip,
            "agent_name": self.agent_name,
            "submitted_at": self.submitted_at,
            "queue": self.queue,
            "runtime": self.runtime_str(),
            "reserve_timeout": self.reserve_timeout,
            "reserve_timeout_display": self.reserve_timeout_str(),
        }


def _fmt_duration(total_secs: int) -> str:
    """Format a duration in seconds as e.g. ``3d 2h 15m``."""
    days, remainder = divmod(total_secs, 86400)
    hours, remainder = divmod(remainder, 3600)
    mins, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _parse_submission_time(time_str: str) -> datetime | None:
    """Parse submission time like ``Thu Apr 23 18:33``.

    The year is not included in testflinger output — we assume the
    current year and fall back to the previous year if the result
    would be in the future.
    """
    try:
        now = datetime.now(tz=timezone.utc)
        dt = datetime.strptime(time_str, "%a %b %d %H:%M")
        dt = dt.replace(year=now.year, tzinfo=timezone.utc)
        if dt > now:
            dt = dt.replace(year=now.year - 1)
        return dt
    except ValueError:
        return None


# ------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------


def list_jobs(
    all_jobs: bool = False,
    output_format: str = "table",
) -> int:
    """List Testflinger jobs with status, IP, runtime, and reserved time.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    try:
        jobs = _fetch_jobs(all_jobs=all_jobs)

        if output_format == "json":
            _output_json(jobs)
        else:
            _output_table(jobs)

        return 0

    except Exception as exc:
        log.error("Failed to list jobs: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------
# Data fetching
# ------------------------------------------------------------------


def _fetch_jobs(all_jobs: bool = False) -> list[JobInfo]:
    """Fetch Testflinger jobs, merging data from multiple CLI calls.

    Strategy:
      1. ``testflinger jobs --status`` — one call gives job IDs,
         statuses, submission times, and queues.  May return
         partial results if the server is unreachable mid-stream.
      2. ``testflinger jobs`` (plain, no server call) — fills in
         any jobs that were missed above.
      3. For each active job: ``testflinger results`` for device_ip
         / agent_name, and ``testflinger show`` for reserve_timeout.
    """
    jobs_map: dict[str, JobInfo] = {}

    # 1. Try ``testflinger jobs --status`` (inline status)
    result_s = run_local(
        ["testflinger", "jobs", "--status"],
        stream=False,
        timeout=60,
    )
    if result_s.stdout:
        for line in result_s.stdout.splitlines():
            job = _parse_status_line(line)
            if job:
                jobs_map[job.job_id] = job

    # 2. Supplement with ``testflinger jobs`` (reliable local cache)
    result_p = run_local(
        ["testflinger", "jobs"],
        stream=False,
        timeout=30,
    )
    if result_p.ok:
        for line in result_p.stdout.splitlines():
            job = _parse_plain_line(line)
            if job and job.job_id not in jobs_map:
                jobs_map[job.job_id] = job

    if not jobs_map:
        raise RuntimeError(
            "No jobs returned by testflinger — "
            "are you logged in? (testflinger login)"
        )

    jobs = list(jobs_map.values())

    # 3. Filter
    if not all_jobs:
        jobs = [
            j for j in jobs if j.status in _ACTIVE_PHASES or j.status is None
        ]

    # 4. Enrich with results and show data
    for job in jobs:
        _enrich_job(job)

    return jobs


def _parse_status_line(line: str) -> JobInfo | None:
    """Parse one line of ``testflinger jobs --status`` output."""
    m = _LINE_STATUS_RE.match(line.strip())
    if not m:
        return None
    job_id, status, sub_time, queue = m.groups()
    return JobInfo(
        job_id=job_id,
        status=status.lower(),
        submitted_at=sub_time,
        submitted_dt=_parse_submission_time(sub_time),
        queue=queue,
    )


def _parse_plain_line(line: str) -> JobInfo | None:
    """Parse one line of ``testflinger jobs`` output (no status)."""
    m = _LINE_PLAIN_RE.match(line.strip())
    if not m:
        return None
    job_id, sub_time, queue = m.groups()
    return JobInfo(
        job_id=job_id,
        submitted_at=sub_time,
        submitted_dt=_parse_submission_time(sub_time),
        queue=queue,
    )


def _enrich_job(job: JobInfo) -> None:
    """Add device_ip, agent_name, and reserve_timeout from the server."""
    # results → device_ip, agent_name
    try:
        results = testflinger.get_job_results(job.job_id)
        info = results.get("device_info", {})
        job.device_ip = info.get("device_ip")
        job.agent_name = info.get("agent_name")
    except Exception as exc:
        log.debug(
            "Could not fetch results for %s: %s",
            job.job_id[:8],
            exc,
        )

    # show → reserve_timeout
    try:
        show = _get_job_show(job.job_id)
        timeout = show.get("reserve_data", {}).get("timeout") or show.get(
            "global_timeout"
        )
        if timeout is not None:
            job.reserve_timeout = int(timeout)
    except Exception as exc:
        log.debug(
            "Could not fetch show for %s: %s",
            job.job_id[:8],
            exc,
        )


def _get_job_show(job_id: str) -> dict:
    """Retrieve the original job definition via ``testflinger show``."""
    result = run_local(
        ["testflinger", "show", job_id],
        stream=False,
        timeout=30,
    )
    if not result.ok:
        return {}
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------


def _output_table(jobs: list[JobInfo]) -> None:
    """Print jobs as a formatted table."""
    if not jobs:
        print("No jobs found")
        return

    # Pre-compute display values
    cols = {
        "job_id": ("Job ID", [j.job_id for j in jobs]),
        "status": (
            "Status",
            [j.status or "unknown" for j in jobs],
        ),
        "ip": (
            "IP Address",
            [j.device_ip or "N/A" for j in jobs],
        ),
        "agent": (
            "Agent",
            [j.agent_name or "N/A" for j in jobs],
        ),
        "runtime": (
            "Runtime",
            [j.runtime_str() for j in jobs],
        ),
        "reserved": (
            "Reserved",
            [j.reserve_timeout_str() for j in jobs],
        ),
    }

    widths = {
        k: max(len(header), max(len(v) for v in vals))
        for k, (header, vals) in cols.items()
    }

    col_order = [
        "job_id",
        "status",
        "ip",
        "agent",
        "runtime",
        "reserved",
    ]

    header = "  ".join(cols[c][0].ljust(widths[c]) for c in col_order)
    print(header)
    print("─" * len(header))

    for i in range(len(jobs)):
        row = "  ".join(cols[c][1][i].ljust(widths[c]) for c in col_order)
        print(row)


def _output_json(jobs: list[JobInfo]) -> None:
    """Print jobs as JSON."""
    output = {
        "jobs": [j.to_dict() for j in jobs],
        "count": len(jobs),
    }
    print(json.dumps(output, indent=2))
