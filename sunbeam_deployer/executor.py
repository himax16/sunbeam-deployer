"""Command execution for host and LXD VMs.

Provides a unified interface for running commands locally and inside LXD
containers/VMs, with streaming output, timeouts, and retry support.

When a remote SSH target is configured (via ``set_remote_target``), all
``run_host`` / ``run_in_vm`` / ``push_file_to_vm`` calls are transparently
routed through SSH so that existing phase code works unchanged.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("sunbeam_deployer.executor")


# ---------------------------------------------------------------------------
# Remote target (SSH) — module-level state
# ---------------------------------------------------------------------------


@dataclass
class RemoteTarget:
    """SSH connection details for a remote machine."""

    host: str
    user: str = "ubuntu"
    key_path: str | None = None
    ssh_options: list[str] | None = None

    @property
    def ssh_base(self) -> list[str]:
        """Base SSH command components."""
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.ssh_options:
            cmd.extend(self.ssh_options)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    @property
    def scp_base(self) -> list[str]:
        """Base SCP command components."""
        cmd = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        return cmd


_remote_target: RemoteTarget | None = None


def set_remote_target(target: RemoteTarget | None) -> None:
    """Set or clear the remote SSH target for all host commands."""
    global _remote_target
    _remote_target = target
    if target:
        log.info("Remote target set: %s@%s", target.user, target.host)
    else:
        log.info("Remote target cleared — commands run locally")


def get_remote_target() -> RemoteTarget | None:
    """Return the current remote target, if any."""
    return _remote_target


@dataclass
class CommandResult:
    """Result of a command execution."""

    returncode: int
    stdout: str
    stderr: str
    duration: float  # seconds
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_local(
    cmd: str | list[str],
    *,
    timeout: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stream: bool = True,
    check: bool = False,
    input_text: str | None = None,
) -> CommandResult:
    """Run a command on the local machine (never SSH-wrapped).

    Used for testflinger CLI calls and other local-only operations.
    """
    return _run_subprocess(
        cmd,
        timeout=timeout,
        cwd=cwd,
        env=env,
        stream=stream,
        check=check,
        input_text=input_text,
        log_prefix="local",
    )


def run_host(
    cmd: str | list[str],
    *,
    timeout: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stream: bool = True,
    check: bool = False,
    input_text: str | None = None,
) -> CommandResult:
    """Run a command on the deployment host.

    If a remote target is configured, the command is executed over SSH.
    If *stream* is ``True`` stdout/stderr lines are logged as they arrive.
    If *check* is ``True`` a non-zero exit raises ``RuntimeError``.
    """
    if _remote_target is not None:
        return _run_via_ssh(
            cmd,
            timeout=timeout,
            stream=stream,
            check=check,
            input_text=input_text,
        )

    return _run_subprocess(
        cmd,
        timeout=timeout,
        cwd=cwd,
        env=env,
        stream=stream,
        check=check,
        input_text=input_text,
        log_prefix="host",
    )


def _run_via_ssh(
    cmd: str | list[str],
    *,
    timeout: int | None = None,
    stream: bool = True,
    check: bool = False,
    input_text: str | None = None,
) -> CommandResult:
    """Execute a command on the remote target over SSH."""
    assert _remote_target is not None

    if isinstance(cmd, list):
        remote_cmd = " ".join(shlex.quote(c) for c in cmd)
    else:
        remote_cmd = cmd

    ssh_cmd = _remote_target.ssh_base + [remote_cmd]
    log.debug("ssh[%s]$ %s", _remote_target.host, remote_cmd)
    return _run_subprocess(
        ssh_cmd,
        timeout=timeout,
        stream=stream,
        check=check,
        input_text=input_text,
        log_prefix=f"ssh[{_remote_target.host}]",
    )


def _run_subprocess(
    cmd: str | list[str],
    *,
    timeout: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stream: bool = True,
    check: bool = False,
    input_text: str | None = None,
    log_prefix: str = "host",
) -> CommandResult:
    """Low-level subprocess execution."""
    if isinstance(cmd, str):
        shell = True
        display_cmd = cmd
    else:
        shell = False
        display_cmd = " ".join(cmd)

    log.debug("%s$ %s", log_prefix, display_cmd)
    start = time.monotonic()
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
        )

        stdout_lines: list[str] = []

        if input_text:
            proc.stdin.write(input_text)
            proc.stdin.close()
            proc.stdin = None  # prevent communicate() from re-closing

        if stream and proc.stdout:
            for line in proc.stdout:
                line = line.rstrip("\n")
                stdout_lines.append(line)
                log.debug("  | %s", line)

        remaining_stdout, _ = proc.communicate(timeout=timeout)
        if remaining_stdout:
            for line in remaining_stdout.splitlines():
                stdout_lines.append(line)
                if stream:
                    log.debug("  | %s", line)

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
        stdout_lines.append(f"<TIMEOUT after {timeout}s>")

    duration = time.monotonic() - start
    result = CommandResult(
        returncode=proc.returncode,
        stdout="\n".join(stdout_lines),
        stderr="",
        duration=duration,
        timed_out=timed_out,
    )

    if not result.ok:
        log.debug(
            "Command exited %d (%.1fs)%s",
            result.returncode,
            duration,
            " [TIMEOUT]" if timed_out else "",
        )
    if check and not result.ok:
        raise RuntimeError(
            f"Command failed (rc={result.returncode}): "
            f"{display_cmd}\n{result.stdout[-2000:]}"
        )

    return result


def run_in_vm(
    vm_name: str,
    cmd: str,
    *,
    user: str = "ubuntu",
    timeout: int | None = None,
    stream: bool = True,
    check: bool = False,
) -> CommandResult:
    """Run a command inside an LXD VM as the given user.

    Uses ``lxc exec <vm> -- sudo -iu <user> bash -c '<cmd>'``.
    """
    wrapped = f"sudo -iu {user} bash -lc {_shell_quote(cmd)}"
    lxc_cmd = ["lxc", "exec", vm_name, "--", "bash", "-c", wrapped]
    log.debug("vm[%s]$ %s", vm_name, cmd)
    return run_host(lxc_cmd, timeout=timeout, stream=stream, check=check)


def push_file_to_vm(
    vm_name: str,
    local_path: str,
    remote_path: str,
    *,
    check: bool = True,
) -> CommandResult:
    """Push a file into an LXD VM.

    When running remotely, the file is first copied to the SSH host, then
    pushed into the VM via ``lxc file push``.
    """
    if _remote_target is not None:
        # First SCP the file to the remote host
        scp_result = scp_to_remote(local_path, f"/tmp/_deployer_push_{vm_name}")
        if not scp_result.ok:
            return scp_result
        # Then push from remote host into the VM
        return run_host(
            f"lxc file push /tmp/_deployer_push_{vm_name}"
            f" {vm_name}{remote_path}"
            f" && rm -f /tmp/_deployer_push_{vm_name}",
            check=check,
        )

    return run_host(
        ["lxc", "file", "push", local_path, f"{vm_name}{remote_path}"],
        check=check,
    )


def scp_to_remote(
    local_path: str, remote_path: str, *, check: bool = True
) -> CommandResult:
    """Copy a local file to the remote SSH target."""
    assert _remote_target is not None
    cmd = _remote_target.scp_base + [
        local_path,
        f"{_remote_target.user}@{_remote_target.host}:{remote_path}",
    ]
    return _run_subprocess(cmd, check=check, log_prefix="scp")


def wait_for_ssh(
    host: str, user: str, key_path: str | None = None, timeout: int = 600
) -> bool:
    """Wait until an SSH connection to *host* succeeds."""
    log.info("Waiting for SSH on %s@%s…", user, host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
        ]
        if key_path:
            ssh_cmd.extend(["-i", key_path])
        ssh_cmd.extend([f"{user}@{host}", "echo ready"])

        result = _run_subprocess(
            ssh_cmd, timeout=15, stream=False, log_prefix="ssh-probe"
        )
        if result.ok and "ready" in result.stdout:
            log.info("SSH ready on %s", host)
            return True
        time.sleep(10)
    log.error("SSH not ready on %s within %ds", host, timeout)
    return False


def wait_for_vm(vm_name: str, timeout: int = 300) -> bool:
    """Wait until a VM's agent is responsive."""
    log.info("Waiting for VM %s to become ready…", vm_name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run_host(
            ["lxc", "exec", vm_name, "--", "echo", "ready"],
            timeout=10,
            stream=False,
        )
        if result.ok and "ready" in result.stdout:
            log.info("VM %s is responsive", vm_name)
            return True
        time.sleep(5)
    log.error("VM %s not ready within %ds", vm_name, timeout)
    return False


def wait_for_cloud_init(vm_name: str, timeout: int = 600) -> bool:
    """Wait for cloud-init to finish inside a VM."""
    log.info("Waiting for cloud-init on %s…", vm_name)
    result = run_in_vm(
        vm_name,
        "cloud-init status --wait",
        timeout=timeout,
        stream=True,
        user="root",
    )
    if result.ok:
        log.info("cloud-init complete on %s", vm_name)
        return True
    log.error("cloud-init failed/timed-out on %s", vm_name)
    return False


def with_retry(
    fn: Callable[[], CommandResult],
    *,
    retries: int = 3,
    delay: int = 10,
    label: str = "command",
) -> CommandResult:
    """Retry a command-returning callable up to *retries* times."""
    last_result: CommandResult | None = None
    for attempt in range(1, retries + 1):
        last_result = fn()
        if last_result.ok:
            return last_result
        log.warning(
            "%s failed (attempt %d/%d, rc=%d), retrying in %ds…",
            label,
            attempt,
            retries,
            last_result.returncode,
            delay,
        )
        if attempt < retries:
            time.sleep(delay)
    assert last_result is not None
    return last_result


def _shell_quote(s: str) -> str:
    """Single-quote a string for bash -c."""
    return "'" + s.replace("'", "'\\''") + "'"
