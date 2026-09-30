"""Run the live feature tests against a deployed cluster.

Resolves the Grafana URL and admin password from the primary VM, opens an
SSH port-forward when the deployment host is remote (Grafana lives on the
LXD network, unreachable from the agent), then runs pytest with
pytest-playwright on the bundled test modules. Results are written as a
JUnit report next to the screenshots and summarised in the log file.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sunbeam_deployer.executor import get_remote_target, run_in_vm

log = logging.getLogger("sunbeam_deployer.testing.runner")

_TESTS_DIR = Path(__file__).resolve().parent
_OBSERVABILITY_TESTS = _TESTS_DIR / "test_grafana_dashboards.py"
_SCREENSHOTS_ROOT = "~/.local/share/sunbeam-deployer/screenshots"


def default_artifacts_dir() -> Path:
    """Return a fresh timestamped local directory for test artifacts."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path(_SCREENSHOTS_ROOT).expanduser() / stamp


@dataclass
class TestReport:
    """Outcome of a pytest run, parsed from its JUnit XML report."""

    total: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.passed)} passed, {len(self.failed)} failed, "
            f"{len(self.skipped)} skipped (of {self.total})"
        )


def parse_junit_report(path: Path) -> TestReport:
    """Parse a pytest ``--junitxml`` report into a :class:`TestReport`."""
    report = TestReport()
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name", "?")
        report.total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            report.failed.append(name)
        elif case.find("skipped") is not None:
            report.skipped.append(name)
        else:
            report.passed.append(name)
    return report


def missing_dependencies() -> list[str]:
    """Return the ``testing`` extra modules that are not importable."""
    return [
        mod
        for mod in ("pytest", "pytest_playwright")
        if importlib.util.find_spec(mod) is None
    ]


def grafana_credentials(vm_name: str) -> tuple[str, str]:
    """Return ``(url, admin-password)`` from the grafana leader action."""
    result = run_in_vm(
        vm_name,
        "juju run -m observability grafana/leader get-admin-password "
        "--format json",
        stream=False,
        timeout=180,
    )
    if not result.ok:
        raise RuntimeError(
            "Failed to get Grafana admin password — is observability "
            "enabled? See the log file for details."
        )
    # juju may print progress lines ("Running operation…") before the JSON.
    lines = result.stdout.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), 0)
    try:
        data = json.loads("\n".join(lines[start:]))
        results = next(iter(data.values()))["results"]
        return (results["url"], results["admin-password"])
    except (ValueError, KeyError, StopIteration) as exc:
        raise RuntimeError(
            f"Unexpected get-admin-password output: {exc}"
        ) from exc


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


@contextmanager
def reachable_url(url: str) -> Iterator[str]:
    """Yield *url*, rewritten through an SSH tunnel if the host is remote."""
    target = get_remote_target()
    if target is None:
        yield url
        return

    parsed = urllib.parse.urlsplit(url)
    remote_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    local_port = _free_port()
    cmd = target.ssh_base[:-1] + [
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"127.0.0.1:{local_port}:{parsed.hostname}:{remote_port}",
        target.ssh_base[-1],
    ]
    log.info(
        "Forwarding 127.0.0.1:%d -> %s:%d via %s",
        local_port,
        parsed.hostname,
        remote_port,
        target.host,
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        if not _wait_port(local_port, timeout=30):
            proc.terminate()
            _, err = proc.communicate(timeout=10)
            raise RuntimeError(
                f"SSH port-forward failed: {err.decode().strip()}"
            )
        yield urllib.parse.urlunsplit(
            parsed._replace(netloc=f"127.0.0.1:{local_port}")
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_observability_tests(
    vm_name: str,
    *,
    artifacts_dir: str | None = None,
    headed: bool = False,
    pytest_args: tuple[str, ...] = (),
) -> int:
    """Run the Grafana dashboard tests; return pytest's exit code."""
    url, password = grafana_credentials(vm_name)
    log.info("Grafana URL: %s", url)

    # Local path (screenshots are saved on the agent machine).
    out_dir = (
        Path(artifacts_dir).expanduser()
        if artifacts_dir
        else default_artifacts_dir()
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "report.xml"

    with reachable_url(url) as local_url:
        env = dict(os.environ)
        env.update(
            GRAFANA_URL=local_url,
            GRAFANA_PASSWORD=password,
            TESTING_ARTIFACTS_DIR=str(out_dir),
        )
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(_OBSERVABILITY_TESTS),
            f"--rootdir={_TESTS_DIR}",
            "-p",
            "no:cacheprovider",
            "-v",
            "--browser",
            "chromium",
            "--screenshot",
            "only-on-failure",
            "--output",
            str(out_dir / "failures"),
            f"--junitxml={report_path}",
        ]
        if headed:
            cmd.append("--headed")
        cmd.extend(pytest_args)
        log.debug("Running: %s", " ".join(cmd))
        rc = subprocess.run(cmd, env=env).returncode

    _log_report(report_path)
    log.info("Screenshots and report saved to %s", out_dir)
    return rc


def _log_report(path: Path) -> None:
    """Record the per-test outcome and summary in the log file."""
    try:
        report = parse_junit_report(path)
    except (OSError, ET.ParseError) as exc:
        log.warning("No test report at %s: %s", path, exc)
        return
    for name in report.passed:
        log.debug("PASSED %s", name)
    for name in report.skipped:
        log.info("SKIPPED %s", name)
    for name in report.failed:
        log.error("FAILED %s", name)
    log.info("Test results: %s — report: %s", report.summary, path)
