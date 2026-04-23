"""Phase 0 — Testflinger: submit or attach to a job, wait for provisioning.

This phase runs *before* host-setup.  When complete it configures the
executor's SSH remote target so that all subsequent ``run_host`` calls
execute on the Testflinger machine.

Reference: https://canonical-testflinger.readthedocs-hosted.com/latest/
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

import yaml

from sunbeam_deployer.config import DeployConfig
from sunbeam_deployer.executor import (
    RemoteTarget,
    run_local,
    set_remote_target,
    wait_for_ssh,
)
from sunbeam_deployer.monitor import DeploymentMonitor, Status

log = logging.getLogger("sunbeam_deployer.phases.testflinger")

PHASE = "testflinger"

# Testflinger job phases run in this order:
#   setup → provision → firmware_update → test → allocate → reserve → cleanup
# We consider the job "ready" once it reaches the reserve phase.
_WAITING_PHASES = {
    "waiting",
    "queued",
    "setup",
    "provision",
    "firmware_update",
    "allocate",
}
_READY_PHASES = {"reserve", "test"}
_TERMINAL_PHASES = {"cancelled", "complete", "completed", "cleanup"}


def run_phase(cfg: DeployConfig, mon: DeploymentMonitor) -> str:
    """Submit or attach to a Testflinger job and return the device IP.

    After this phase the executor's remote target is configured.
    """
    mon.add_phase(PHASE)
    mon.start_phase(PHASE)

    try:
        tf = cfg.testflinger

        if tf.job_id:
            job_id = tf.job_id
            log.info("Attaching to existing Testflinger job: %s", job_id)
        else:
            job_id = _submit_job(cfg, mon)

        device_ip = _wait_for_ready(cfg, mon, job_id)

        _setup_ssh(cfg, mon, device_ip)

        # Store job_id back in config for reference
        tf.job_id = job_id

        mon.end_phase(PHASE, Status.SUCCESS)
        return device_ip

    except Exception as exc:
        mon.end_phase(PHASE, Status.FAILED, error=str(exc))
        raise


def cancel_job(job_id: str) -> None:
    """Cancel a Testflinger job to release the machine."""
    log.info("Cancelling Testflinger job %s", job_id)
    result = run_local(
        ["testflinger", "cancel", job_id],
        stream=False,
        timeout=30,
    )
    if result.ok:
        log.info("Job %s cancelled", job_id)
    else:
        log.warning("Failed to cancel job %s: %s", job_id, result.stdout[:200])


def get_job_status(job_id: str) -> str:
    """Query the current status/phase of a Testflinger job."""
    result = run_local(
        ["testflinger", "status", job_id],
        stream=False,
        timeout=30,
    )
    return result.stdout.strip().lower() if result.ok else ""


def get_job_results(job_id: str) -> dict:
    """Retrieve the full results JSON for a Testflinger job."""
    result = run_local(
        ["testflinger", "results", job_id],
        stream=False,
        check=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(
            "Could not parse testflinger results "
            f"as JSON: {result.stdout[:500]}"
        ) from err


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _submit_job(cfg: DeployConfig, mon: DeploymentMonitor) -> str:
    """Submit a new Testflinger job and return the job ID."""
    with mon.run_step(PHASE, "submit-job", "Submit Testflinger job"):
        tf = cfg.testflinger

        if tf.job_file:
            job_file = tf.job_file
            log.info("Submitting job from file: %s", job_file)
        else:
            job_file = _generate_job_yaml(cfg)
            log.info("Generated job YAML: %s", job_file)

        result = run_local(
            ["testflinger", "submit", "--quiet", job_file],
            check=True,
            stream=False,
        )

        # --quiet returns just the job ID
        job_id = result.stdout.strip()
        if not job_id:
            raise RuntimeError(
                f"testflinger submit returned no job ID: {result.stdout}"
            )

        log.info("Submitted Testflinger job: %s", job_id)
        return job_id


def _snap_tmp_dir() -> str:
    """Return a temp directory the testflinger-cli snap can read.

    The snap has private ``/tmp`` so host-created files in
    ``/tmp`` are invisible to it.  Instead we use the snap's
    user-common directory which is always accessible.
    """
    d = os.path.join(
        os.path.expanduser("~"),
        "snap",
        "testflinger-cli",
        "common",
        "tmp",
    )
    os.makedirs(d, exist_ok=True)
    return d


def _generate_job_yaml(cfg: DeployConfig) -> str:
    """Generate a testflinger reserve-job YAML and return its path.

    The file is written to the ``testflinger-cli`` snap's
    user-common tmp directory so the snap process can read it.

    Fields follow the schema at:
    https://canonical-testflinger.readthedocs-hosted.com/latest/reference/job-schema/
    """
    tf = cfg.testflinger
    job_data: dict = {
        "job_queue": tf.queue,
        # Safety net: reservation plus 1h for provisioning
        "global_timeout": tf.reserve_timeout + 3600,
        # 15 min output timeout is the default; be explicit
        "output_timeout": 900,
        "provision_data": {
            "distro": tf.distro,
        },
        "reserve_data": {
            "timeout": tf.reserve_timeout,
        },
    }

    if tf.ssh_keys:
        job_data["reserve_data"]["ssh_keys"] = tf.ssh_keys

    fd, path = tempfile.mkstemp(
        suffix=".yaml",
        prefix="testflinger-job-",
        dir=_snap_tmp_dir(),
    )
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(job_data, fh, default_flow_style=False)

    log.debug("Generated job YAML at %s", path)
    return path


def _wait_for_ready(
    cfg: DeployConfig, mon: DeploymentMonitor, job_id: str
) -> str:
    """Poll the Testflinger job until it reaches 'reserve' and return device IP.

    Testflinger phases run sequentially:
      setup → provision → firmware_update → test → allocate → reserve → cleanup
    We wait for 'reserve' (or 'test' if a test_data job).
    """
    with mon.run_step(PHASE, "wait-provision", "Wait for provisioning"):
        tf = cfg.testflinger
        deadline = time.monotonic() + tf.provision_timeout
        last_state = ""
        poll_interval = 15  # start fast, then slow down

        while time.monotonic() < deadline:
            state = get_job_status(job_id)

            if state != last_state:
                log.info("Job %s phase: %s", job_id[:8], state or "(unknown)")
                last_state = state

            if state in _READY_PHASES:
                return _get_device_ip(job_id)

            if state in _TERMINAL_PHASES:
                raise RuntimeError(
                    f"Testflinger job {job_id} reached "
                    f"terminal state '{state}' "
                    "before provisioning completed"
                )

            # Back off gradually: 15s for first 5 min, then 30s
            elapsed = tf.provision_timeout - (deadline - time.monotonic())
            poll_interval = 15 if elapsed < 300 else 30
            time.sleep(poll_interval)

        raise RuntimeError(
            f"Testflinger job {job_id} did not reach 'reserve' state "
            f"within {tf.provision_timeout}s (last state: {last_state})"
        )


def _get_device_ip(job_id: str, retries: int = 10, delay: float = 5.0) -> str:
    """Extract the device IP from testflinger results.

    The results JSON contains ``device_info.device_ip`` once provisioning
    is complete and the machine is in reserve/test phase.  Testflinger may
    populate this field a few seconds after the job reaches 'reserve', so
    we retry briefly to handle that race condition.
    """
    for attempt in range(retries):
        results = get_job_results(job_id)
        device_ip = results.get("device_info", {}).get("device_ip")
        if device_ip:
            agent_name = results.get("device_info", {}).get(
                "agent_name", "unknown"
            )
            log.info("Device IP: %s (agent: %s)", device_ip, agent_name)
            return device_ip

        if attempt < retries - 1:
            log.debug(
                "device_ip not yet in results for job %s, "
                "retrying in %.0fs (%d/%d)",
                job_id[:8],
                delay,
                attempt + 1,
                retries,
            )
            time.sleep(delay)

    raise RuntimeError(f"No device_ip in testflinger results for job {job_id}")


def _setup_ssh(
    cfg: DeployConfig, mon: DeploymentMonitor, device_ip: str
) -> None:
    """Wait for SSH and configure the executor remote target."""
    with mon.run_step(PHASE, "ssh-connect", f"Establish SSH to {device_ip}"):
        tf = cfg.testflinger

        if not wait_for_ssh(
            device_ip, tf.ssh_user, tf.ssh_key_path, timeout=300
        ):
            raise RuntimeError(
                f"Cannot SSH to {tf.ssh_user}@{device_ip}"
                " — machine not reachable"
            )

        set_remote_target(
            RemoteTarget(
                host=device_ip,
                user=tf.ssh_user,
                key_path=tf.ssh_key_path,
            )
        )
