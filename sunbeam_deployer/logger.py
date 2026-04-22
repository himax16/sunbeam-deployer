"""Dual logging — full detail to file, progress summary to terminal."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Patterns that should be redacted in log output
_SENSITIVE_PATTERNS = [
    re.compile(r"(token[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9+/=_-]{16,})", re.I),
    re.compile(
        r"(-----BEGIN [A-Z ]+KEY-----)(.*?)(-----END [A-Z ]+KEY-----)", re.S
    ),
    re.compile(r"(apikey[\"']?\s*[:=]\s*[\"']?)(\S+)", re.I),
    re.compile(r"(password[\"']?\s*[:=]\s*[\"']?)(\S+)", re.I),
]


def _redact(text: str) -> str:
    """Replace sensitive values with <REDACTED>."""
    for pat in _SENSITIVE_PATTERNS:
        if pat.groups == 3:
            text = pat.sub(r"\1<REDACTED>\3", text)
        else:
            text = pat.sub(r"\1<REDACTED>", text)
    return text


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from all records."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return _redact(original)


class _TerminalFormatter(_RedactingFormatter):
    """Coloured, concise terminal output."""

    COLORS = {
        logging.DEBUG: "\033[90m",  # grey
        logging.INFO: "\033[36m",  # cyan
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",  # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLORS.get(record.levelno, "")
        phase = getattr(record, "phase", "")
        step = getattr(record, "step", "")
        prefix_parts = [p for p in (phase, step) if p]
        prefix = f"[{'/'.join(prefix_parts)}] " if prefix_parts else ""
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = _redact(record.getMessage())
        return (
            f"{colour}{ts} {self.BOLD}{prefix}"
            f"{self.RESET}{colour}{msg}{self.RESET}"
        )


def setup_logging(log_dir: str, verbose: bool = False) -> logging.Logger:
    """Configure the ``sunbeam_deployer`` logger.

    Returns the root logger for the package.
    """
    log_path = Path(os.path.expanduser(log_dir))
    log_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = log_path / f"sunbeam-deploy-{timestamp}.log"

    logger = logging.getLogger("sunbeam_deployer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # File handler — everything, with timestamps
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        _RedactingFormatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        )
    )
    logger.addHandler(fh)

    # Terminal handler — INFO+ by default, DEBUG if verbose
    th = logging.StreamHandler(sys.stderr)
    th.setLevel(logging.DEBUG if verbose else logging.INFO)
    th.setFormatter(_TerminalFormatter())
    logger.addHandler(th)

    logger.info("Log file: %s", log_file)
    return logger


def phase_logger(name: str) -> logging.LoggerAdapter:
    """Return an adapter that tags messages with *phase=name*."""
    logger = logging.getLogger("sunbeam_deployer")
    return logging.LoggerAdapter(logger, {"phase": name, "step": ""})


def step_logger(phase: str, step: str) -> logging.LoggerAdapter:
    """Return an adapter that tags messages with *phase* and *step*."""
    logger = logging.getLogger("sunbeam_deployer")
    return logging.LoggerAdapter(logger, {"phase": phase, "step": step})
