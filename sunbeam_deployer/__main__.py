"""CLI entry point — ``python -m sunbeam_deployer``."""

from __future__ import annotations

import argparse
import logging
import sys

from sunbeam_deployer import __version__
from sunbeam_deployer.commands import list_jobs
from sunbeam_deployer.config import DeployConfig, load_config
from sunbeam_deployer.executor import RemoteTarget, set_remote_target
from sunbeam_deployer.logger import setup_logging
from sunbeam_deployer.monitor import DeploymentMonitor, Status
from sunbeam_deployer.phases import cluster, host_setup, testflinger, vm_deploy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sunbeam-deployer",
        description="Automated Sunbeam deployment on Testflinger machines.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Create subcommands
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
    )

    # deploy command (default behavior, no subcommand required)
    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Deploy Sunbeam on Testflinger machine (default if no command)",
    )
    _add_deploy_args(deploy_parser)

    # list-jobs command
    list_parser = subparsers.add_parser(
        "list-jobs",
        help="List all Testflinger jobs and their IP addresses",
    )
    list_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) output",
    )
    list_parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Show all jobs (including waiting/completed); "
        "by default only shows active/ready jobs",
    )
    list_parser.add_argument(
        "-f",
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )

    # Make deploy args available at top level too (for backward compatibility)
    _add_deploy_args(parser)

    return parser


def _add_deploy_args(parser: argparse.ArgumentParser) -> None:
    """Add deployment-specific arguments to a parser."""
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        help="Path to YAML configuration file (default: use built-in defaults)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) terminal output",
    )

    # Allow running individual phases
    parser.add_argument(
        "--phase",
        choices=["all", "testflinger", "host-setup", "vm-deploy", "cluster"],
        default="all",
        help=(
            "Run a specific phase instead of the full deployment (default: all)"
        ),
    )

    # Testflinger options
    tf_group = parser.add_argument_group(
        "testflinger", "Testflinger machine provisioning"
    )
    tf_group.add_argument(
        "--testflinger",
        action="store_true",
        default=None,
        help="Enable Testflinger provisioning (submit or attach to a job)",
    )
    tf_group.add_argument(
        "--tf-job-id",
        metavar="JOB_ID",
        help="Attach to an existing Testflinger job instead of submitting",
    )
    tf_group.add_argument(
        "--tf-job-file",
        metavar="FILE",
        help="Path to a Testflinger job YAML to submit",
    )
    tf_group.add_argument(
        "--tf-ssh-key",
        metavar="PATH",
        help="SSH private key for connecting to the Testflinger machine",
    )
    tf_group.add_argument(
        "--device-ip",
        metavar="IP",
        help="Skip Testflinger and connect directly to a machine via SSH",
    )

    # Quick overrides for common settings
    parser.add_argument(
        "--snap-channel",
        metavar="CHANNEL",
        help="Override the openstack snap channel (e.g. 2024.1/edge)",
    )
    parser.add_argument(
        "--snap-revision",
        metavar="REV",
        help="Pin to a specific snap revision",
    )
    parser.add_argument(
        "--snap-file",
        metavar="PATH",
        help="Install openstack snap from local .snap file",
    )
    parser.add_argument(
        "--deploy-mode",
        choices=["manual", "maas"],
        help="Override the deploy mode",
    )
    parser.add_argument(
        "--repo-dir",
        metavar="DIR",
        help="Override the directory where the repo is cloned",
    )
    parser.add_argument(
        "--accept-defaults",
        action="store_true",
        default=None,
        help="Pass --accept-defaults to sunbeam bootstrap",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        default=False,
        help="Skip pushing the Terraform-generated manifest to VMs",
    )
    parser.add_argument(
        "--tf-arg",
        action="append",
        metavar="ARG",
        dest="tf_args",
        help="Extra argument passed to terraform apply (repeatable)",
    )
    parser.add_argument(
        "--cancel-on-failure",
        action="store_true",
        default=False,
        help=(
            "Cancel the Testflinger job if "
            "deployment fails (releases the machine)"
        ),
    )


def apply_cli_overrides(cfg: DeployConfig, args: argparse.Namespace) -> None:
    """Apply CLI flags on top of the loaded config."""
    # Testflinger overrides
    if args.testflinger is True:
        cfg.testflinger.enabled = True
    if args.tf_job_id:
        cfg.testflinger.enabled = True
        cfg.testflinger.job_id = args.tf_job_id
    if args.tf_job_file:
        cfg.testflinger.enabled = True
        cfg.testflinger.job_file = args.tf_job_file
    if args.tf_ssh_key:
        cfg.testflinger.ssh_key_path = args.tf_ssh_key
    if args.device_ip:
        # --device-ip skips testflinger entirely, goes straight to SSH
        cfg.testflinger.enabled = False
        cfg._direct_ip = args.device_ip

    # Snap overrides
    if args.snap_channel:
        cfg.snap.channel = args.snap_channel
        cfg.snap.source = "store"
    if args.snap_revision:
        cfg.snap.revision = args.snap_revision
        cfg.snap.source = "store"
    if args.snap_file:
        cfg.snap.local_path = args.snap_file
        cfg.snap.source = "local"
    if args.deploy_mode:
        cfg.deploy_mode = args.deploy_mode
    if args.repo_dir:
        cfg.repo_dir = args.repo_dir
    if args.accept_defaults is True:
        cfg.sunbeam.accept_defaults = True
    if args.no_manifest:
        cfg.sunbeam.manifest = False
    if args.tf_args:
        cfg.terraform.extra_args.extend(args.tf_args)
    if args.verbose:
        cfg.logging.verbose = True


def _prompt_cancel_job(logger: logging.Logger, job_id: str) -> None:
    """Interactively ask whether to cancel a Testflinger job we submitted."""
    logger.info("Testflinger job: %s", job_id)
    try:
        answer = (
            input(
                f"\nCancel Testflinger job {job_id}"
                " and release the machine? [y/N] "
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""
        print()

    if answer in ("y", "yes"):
        testflinger.cancel_job(job_id)
    else:
        logger.info(
            "Job kept alive. Cancel manually: testflinger cancel %s", job_id
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Handle list-jobs command
    if args.command == "list-jobs":
        # Set up minimal logging for list-jobs
        if args.verbose:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(levelname)-8s %(name)s: %(message)s",
            )
        else:
            logging.basicConfig(
                level=logging.WARNING,
                format="%(message)s",
            )

        return list_jobs(
            all_jobs=args.all,
            output_format=args.format,
        )

    # Default to deploy behavior if no command or explicit deploy command
    if args.command not in ("deploy", None):
        parser.print_help()
        return 1

    # Load config
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        return 1

    apply_cli_overrides(cfg, args)

    # Re-validate after overrides
    errors = cfg.validate()
    if errors:
        print("Configuration errors:", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        return 1

    # Set up logging
    logger = setup_logging(cfg.logging.log_dir, cfg.logging.verbose)
    logger.info("Sunbeam Deployer v%s", __version__)
    logger.info("Deploy mode: %s", cfg.deploy_mode)
    logger.info(
        "Snap source: %s (channel=%s)", cfg.snap.source, cfg.snap.channel
    )

    # Set up monitor
    mon = DeploymentMonitor()
    phase = args.phase
    infra = None
    # Track whether we submitted the TF job (vs. attaching to existing)
    submitted_job = False

    try:
        # Phase 0: Testflinger provisioning (or direct SSH)
        direct_ip = getattr(cfg, "_direct_ip", None)

        if phase in ("all", "testflinger") and cfg.testflinger.enabled:
            pre_job_id = cfg.testflinger.job_id
            testflinger.run_phase(cfg, mon)
            if not pre_job_id and cfg.testflinger.job_id:
                submitted_job = True
        elif direct_ip:
            # --device-ip: skip testflinger, set up SSH directly
            from sunbeam_deployer.executor import wait_for_ssh

            logger.info("Connecting directly to %s", direct_ip)
            ssh_user = cfg.testflinger.ssh_user
            ssh_key = cfg.testflinger.ssh_key_path
            if not wait_for_ssh(direct_ip, ssh_user, ssh_key, timeout=120):
                logger.error("Cannot reach %s via SSH", direct_ip)
                return 1
            set_remote_target(
                RemoteTarget(host=direct_ip, user=ssh_user, key_path=ssh_key)
            )
        elif phase == "testflinger":
            logger.error(
                "Testflinger is not enabled — use --testflinger or config"
            )
            return 1

        # Phase 1: Host setup
        if phase in ("all", "host-setup"):
            infra = host_setup.run_phase(cfg, mon)

        # For single-phase runs, reconstruct infra from terraform
        if infra is None and phase in ("vm-deploy", "cluster"):
            logger.info(
                "Reconstructing infrastructure info from Terraform outputs…"
            )
            from sunbeam_deployer.phases.host_setup import PHASE as HS_PHASE
            from sunbeam_deployer.phases.host_setup import (
                _parse_terraform_outputs,
            )

            tmp_mon = DeploymentMonitor()
            tmp_mon.add_phase(HS_PHASE)
            tmp_mon.start_phase(HS_PHASE)
            infra = _parse_terraform_outputs(cfg, tmp_mon)
            tmp_mon.end_phase(HS_PHASE, Status.SUCCESS)

        # Phase 2: VM deployment
        if phase in ("all", "vm-deploy"):
            assert infra is not None
            vm_deploy.run_phase(cfg, mon, infra)

        # Phase 3: Cluster bootstrap + join
        if phase in ("all", "cluster"):
            assert infra is not None
            cluster.run_phase(cfg, mon, infra)

    except Exception as exc:
        logger.error("Deployment failed: %s", exc)

        # Cancel testflinger job on failure if requested
        if args.cancel_on_failure and cfg.testflinger.job_id:
            logger.info("Cancelling Testflinger job due to deployment failure")
            testflinger.cancel_job(cfg.testflinger.job_id)

        print(mon.summary())
        return 1

    print(mon.summary())

    # Post-deployment: prompt to cancel if we submitted the job
    if submitted_job and cfg.testflinger.job_id:
        _prompt_cancel_job(logger, cfg.testflinger.job_id)
    elif cfg.testflinger.job_id:
        logger.info(
            "Testflinger job: %s (use 'testflinger cancel %s' to release)",
            cfg.testflinger.job_id,
            cfg.testflinger.job_id,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
