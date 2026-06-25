"""CLI entry point — ``python -m sunbeam_deployer``."""

from __future__ import annotations

import sys

from sunbeam_deployer.cli import cli


def main() -> int:
    """Main entry point for the CLI."""
    return cli(standalone_mode=False)


if __name__ == "__main__":
    sys.exit(main())
