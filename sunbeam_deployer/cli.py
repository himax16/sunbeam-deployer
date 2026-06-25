"""CLI interface — rich-click group, commands, and deploy logic."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

import rich_click as click
from rich.console import Console

from sunbeam_deployer import __version__
from sunbeam_deployer.commands import list_jobs
from sunbeam_deployer.config import DeployConfig, load_config
from sunbeam_deployer.executor import (
    RemoteTarget,
    get_remote_target,
    set_remote_target,
    wait_for_ssh,
)
from sunbeam_deployer.logger import LiveDisplay, setup_logging
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases import cluster, host_setup, testflinger, vm_deploy

console = Console()

# ---------------------------------------------------------------------------
# Rich-click configuration
# ---------------------------------------------------------------------------

click.rich_click.TEXT_MARKUP = True
click.rich_click.SHOW_ARGUMENTS = False
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_ERRORS_SUGGESTION = "dim italic"
click.rich_click.COMMANDS_BEFORE_OPTIONS = True
click.rich_click.MAX_WIDTH = 100
click.rich_click.COLOR_SYSTEM = "standard"
click.rich_click.STYLE_OPTIONS_PANEL_BORDER = "bold bright_blue"
click.rich_click.STYLE_OPTIONS_PANEL_TITLE_STYLE = "bold bright_blue"
click.rich_click.STYLE_COMMANDS_PANEL_BORDER = "bold bright_blue"
click.rich_click.STYLE_COMMANDS_PANEL_TITLE_STYLE = "bold bright_blue"
click.rich_click.OPTION_GROUPS = {
    "sunbeam-deployer deploy": [
        {
            "name": "General options",
            "options": [
                "--config",
                "--verbose",
                "--phase",
            ],
        },
        {
            "name": "Testflinger options",
            "options": [
                "--testflinger",
                "--tf-job-id",
                "--tf-job-file",
                "--tf-ssh-key",
                "--device-ip",
            ],
        },
        {
            "name": "Snap and deployment overrides",
            "options": [
                "--snap-channel",
                "--snap-revision",
                "--snap-file",
                "--deploy-mode",
                "--repo-dir",
            ],
        },
        {
            "name": "Behaviour flags",
            "options": [
                "--accept-defaults",
                "--no-manifest",
                "--tf-arg",
                "--cancel-on-failure",
            ],
        },
    ],
    "sunbeam-deployer list-jobs": [
        {
            "name": "Options",
            "options": [
                "--verbose",
                "--all",
                "--format",
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Shared deploy options decorator
# ---------------------------------------------------------------------------


def _deploy_options(f: Any) -> Any:
    """Decorator that adds all deploy options to a click command."""
    f = click.option(
        "-c",
        "--config",
        type=click.Path(exists=True, dir_okay=False),
        help=(
            "Path to YAML configuration file (default: use built-in defaults)"
        ),
    )(f)
    f = click.option(
        "-v",
        "--verbose",
        is_flag=True,
        help="Enable verbose terminal output",
    )(f)
    f = click.option(
        "--phase",
        default="all",
        help=(
            "Comma-separated list of phases to run: "
            "testflinger,host-setup,vm-deploy,cluster "
            "or 'all' for everything (default: all)"
        ),
    )(f)
    f = click.option(
        "--testflinger",
        is_flag=True,
        default=None,
        help="Enable Testflinger provisioning (submit or attach to a job)",
    )(f)
    f = click.option(
        "--tf-job-id",
        metavar="JOB_ID",
        help="Attach to an existing Testflinger job instead of submitting",
    )(f)
    f = click.option(
        "--tf-job-file",
        type=click.Path(exists=True, dir_okay=False),
        metavar="FILE",
        help="Path to a Testflinger job YAML to submit",
    )(f)
    f = click.option(
        "--tf-ssh-key",
        type=click.Path(exists=True, dir_okay=False),
        metavar="PATH",
        help="SSH private key for connecting to the Testflinger machine",
    )(f)
    f = click.option(
        "--device-ip",
        metavar="IP",
        help="Skip Testflinger and connect directly to a machine via SSH",
    )(f)
    f = click.option(
        "--snap-channel",
        metavar="CHANNEL",
        help="Override the openstack snap channel (e.g. 2024.1/edge)",
    )(f)
    f = click.option(
        "--snap-revision",
        metavar="REV",
        help="Pin to a specific snap revision",
    )(f)
    f = click.option(
        "--snap-file",
        type=click.Path(exists=True, dir_okay=False),
        metavar="PATH",
        help="Install openstack snap from local .snap file",
    )(f)
    f = click.option(
        "--deploy-mode",
        type=click.Choice(["manual", "maas"]),
        help="Override the deploy mode",
    )(f)
    f = click.option(
        "--repo-dir",
        metavar="DIR",
        help="Override the directory where the repo is cloned",
    )(f)
    f = click.option(
        "--accept-defaults",
        is_flag=True,
        default=None,
        help="Pass --accept-defaults to sunbeam bootstrap",
    )(f)
    f = click.option(
        "--no-manifest",
        is_flag=True,
        default=False,
        help="Skip pushing the Terraform-generated manifest to VMs",
    )(f)
    f = click.option(
        "--tf-arg",
        multiple=True,
        metavar="ARG",
        help="Extra argument passed to terraform apply (repeatable)",
    )(f)
    f = click.option(
        "--cancel-on-failure",
        is_flag=True,
        default=False,
        help=(
            "Cancel the Testflinger job if "
            "deployment fails (releases the machine)"
        ),
    )(f)
    return f


# ---------------------------------------------------------------------------
# Click group and commands
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Automated Sunbeam deployment on Testflinger machines."""


@cli.command("deploy")
@_deploy_options
@click.pass_context
def deploy_cmd(ctx: click.Context, **kwargs: Any) -> None:
    """Deploy Sunbeam."""
    ctx.exit(_run_deploy(kwargs))


@cli.command("list-jobs")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose output",
)
@click.option(
    "-a",
    "--all",
    "all_jobs",
    is_flag=True,
    help=(
        "Show all jobs (including waiting/completed); "
        "by default only shows active/ready jobs"
    ),
)
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format (default: table)",
)
def list_jobs_cmd(verbose: bool, all_jobs: bool, output_format: str) -> None:
    """List all Testflinger jobs and their IP addresses."""
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)-8s %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s",
        )
    list_jobs(all_jobs=all_jobs, output_format=output_format)


# ---------------------------------------------------------------------------
# Shared deploy logic
# ---------------------------------------------------------------------------


def apply_cli_overrides(cfg: DeployConfig, cli_args: dict[str, Any]) -> None:
    """Apply CLI flags on top of the loaded config."""
    # Testflinger overrides
    if cli_args.get("testflinger") is True:
        cfg.testflinger.enabled = True
    if cli_args.get("tf_job_id"):
        cfg.testflinger.enabled = True
        cfg.testflinger.job_id = cli_args["tf_job_id"]
    if cli_args.get("tf_job_file"):
        cfg.testflinger.enabled = True
        cfg.testflinger.job_file = cli_args["tf_job_file"]
    if cli_args.get("tf_ssh_key"):
        cfg.testflinger.ssh_key_path = cli_args["tf_ssh_key"]
    if cli_args.get("device_ip"):
        cfg.testflinger.enabled = False
        cfg.device_ip = cli_args["device_ip"]

    # Snap overrides
    if cli_args.get("snap_channel"):
        cfg.snap.channel = cli_args["snap_channel"]
        cfg.snap.source = "store"
    if cli_args.get("snap_revision"):
        cfg.snap.revision = cli_args["snap_revision"]
        cfg.snap.source = "store"
    if cli_args.get("snap_file"):
        cfg.snap.local_path = cli_args["snap_file"]
        cfg.snap.source = "local"
    if cli_args.get("deploy_mode"):
        cfg.deploy_mode = cli_args["deploy_mode"]
    if cli_args.get("repo_dir"):
        cfg.repo_dir = cli_args["repo_dir"]
    if cli_args.get("accept_defaults") is True:
        cfg.sunbeam.accept_defaults = True
    if cli_args.get("no_manifest"):
        cfg.sunbeam.manifest = False
    if cli_args.get("tf_arg"):
        cfg.terraform.extra_args.extend(cli_args["tf_arg"])
    if cli_args.get("verbose"):
        cfg.logging.verbose = True


def _prompt_cancel_job(logger: logging.Logger, job_id: str) -> None:
    """Interactively ask whether to cancel a TF job we submitted."""
    try:
        answer = input(
            f"Cancel Testflinger job {job_id} and release the machine? [y/N] "
        )
    except EOFError:
        logger.info(
            "Non-interactive session — job %s remains active. "
            "Cancel manually: testflinger cancel %s",
            job_id,
            job_id,
        )
        return
    if answer.strip().lower() in ("y", "yes"):
        logger.info("Cancelling Testflinger job %s…", job_id)
        testflinger.cancel_job(job_id)
    else:
        logger.info(
            "Keeping Testflinger job %s alive. "
            "Cancel manually: testflinger cancel %s",
            job_id,
            job_id,
        )


def _run_deploy(cli_args: dict[str, Any]) -> int:
    """Core deploy logic shared by top-level and deploy subcommand."""
    # Load config
    try:
        cfg = load_config(cli_args.get("config"))
    except Exception as exc:
        console.print(f"[red]Error loading config: {exc}[/]")
        return 1

    apply_cli_overrides(cfg, cli_args)

    # Re-validate after overrides
    errors = cfg.validate()
    if errors:
        console.print("[red]Configuration errors:[/]")
        for e in errors:
            console.print(f"  [red]•[/] {e}")
        return 1

    # Set up logging — skip live display in verbose mode
    display = None if cfg.logging.verbose else LiveDisplay()
    logger = setup_logging(
        cfg.logging.log_dir, cfg.logging.verbose, display=display
    )
    logger.info("Deploy mode: %s", cfg.deploy_mode)
    if cfg.snap.source == "local":
        logger.info("Snap source: %s", cfg.snap.local_path)
    else:
        logger.info("Snap source: Snapstore channel=%s", cfg.snap.channel)

    # Parse --phase into a list
    all_phases = ["testflinger", "host-setup", "vm-deploy", "cluster"]
    phase_raw = cli_args.get("phase", "all")
    if phase_raw == "all":
        phases = list(all_phases)
    else:
        phases = [p.strip() for p in phase_raw.split(",")]
        invalid = [p for p in phases if p not in all_phases]
        if invalid:
            logger.error(
                "Invalid phase(s): %s. Valid: %s",
                ", ".join(invalid),
                ", ".join(all_phases),
            )
            return 1
    if (
        "testflinger" in phases
        and not cfg.testflinger.enabled
        and not cfg.device_ip
    ):
        cfg.testflinger.enabled = True

    # Set up monitor
    mon = DeploymentMonitor()
    infra = None
    submitted_job = False
    failed = False

    ctx = display or nullcontext()
    with ctx:
        try:
            # Phase 0: Testflinger provisioning (or direct SSH)
            direct_ip = cfg.device_ip

            if "testflinger" in phases and cfg.testflinger.enabled:
                pre_job_id = cfg.testflinger.job_id
                testflinger.run_phase(cfg, mon)
                if not pre_job_id and cfg.testflinger.job_id:
                    submitted_job = True
            elif direct_ip:
                logger.info("Connecting directly to %s", direct_ip)
                ssh_user = cfg.testflinger.ssh_user
                ssh_key = cfg.testflinger.ssh_key_path
                if not wait_for_ssh(direct_ip, ssh_user, ssh_key, timeout=120):
                    logger.error("Cannot reach %s via SSH", direct_ip)
                    failed = True
                else:
                    set_remote_target(
                        RemoteTarget(
                            host=direct_ip,
                            user=ssh_user,
                            key_path=ssh_key,
                        )
                    )

            # Phase 1: Host setup
            if not failed and "host-setup" in phases:
                infra = host_setup.run_phase(cfg, mon)

            # For single-phase runs, reconstruct infra from terraform
            if infra is None and ("vm-deploy" in phases or "cluster" in phases):
                logger.info(
                    "Reconstructing infrastructure info from Terraform outputs…"
                )
                from sunbeam_deployer.phases.host_setup import (
                    PHASE as HS_PHASE,
                )
                from sunbeam_deployer.phases.host_setup import (
                    _parse_terraform_outputs,
                )

                tmp_mon = DeploymentMonitor()
                tmp_mon.add_phase(HS_PHASE)
                tmp_mon.start_phase(HS_PHASE)
                infra = _parse_terraform_outputs(cfg, tmp_mon)
                tmp_mon.end_phase(HS_PHASE, Status.SUCCESS)

            # Phase 2: VM deployment
            if not failed and "vm-deploy" in phases:
                assert infra is not None
                vm_deploy.run_phase(cfg, mon, infra)

            # Phase 3: Cluster bootstrap + join
            if not failed and "cluster" in phases:
                assert infra is not None
                cluster.run_phase(cfg, mon, infra)

        except Exception as exc:
            logger.error("Deployment failed: %s", exc)
            failed = True

            if cli_args.get("cancel_on_failure") and cfg.testflinger.job_id:
                logger.info(
                    "Cancelling Testflinger job due to deployment failure"
                )
                testflinger.cancel_job(cfg.testflinger.job_id)
    if submitted_job and cfg.testflinger.job_id:
        _prompt_cancel_job(logger, cfg.testflinger.job_id)
    elif cfg.testflinger.job_id:
        remote_target = get_remote_target()
        if remote_target:
            logger.info(
                "Allocated Testflinger job: %s on %s@%s",
                cfg.testflinger.job_id,
                remote_target.user,
                remote_target.host,
            )
        else:
            logger.info(
                "Testflinger job: %s (failed to get connection info)",
                cfg.testflinger.job_id,
            )
    console.print(mon.summary())
    if failed:
        return 1

    return 0
