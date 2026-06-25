"""Dual logging — rich live display for terminal, concise plain-text file."""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.spinner import Spinner
from rich.text import Text

console = Console()

# Per-thread indentation depth (kept for API compat; unused by RichHandler)
_indent_depth: ContextVar[int] = ContextVar("log_indent", default=0)
_log_file_path: Path | None = None


def set_log_indent(depth: int) -> None:
    _indent_depth.set(depth)


def get_log_indent() -> int:
    return _indent_depth.get()


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    re.compile(r"(token[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9+/=_-]{16,})", re.I),
    re.compile(
        r"(-----BEGIN [A-Z ]+KEY-----)(.*?)(-----END [A-Z ]+KEY-----)",
        re.S,
    ),
    re.compile(r"(apikey[\"']?\s*[:=]\s*[\"']?)(\S+)", re.I),
    re.compile(r"(password[\"']?\s*[:=]\s*[\"']?)(\S+)", re.I),
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _redact(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    for pat in _SENSITIVE_PATTERNS:
        if pat.groups == 3:
            text = pat.sub(r"\1<REDACTED>\3", text)
        else:
            text = pat.sub(r"\1<REDACTED>", text)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


# ---------------------------------------------------------------------------
# Rich live display (spinner + rolling events)
# ---------------------------------------------------------------------------

_MAX_LINES = 8


class LiveDisplay:
    """Spinner at top + scrolling event lines below.

    Use as a context manager for automatic cleanup::

        with LiveDisplay() as display:
            ...
        # live region auto-cleared
    """

    def __init__(self) -> None:
        self._spinner_text = "Preparing\u2026"
        self._events: deque[str] = deque(maxlen=_MAX_LINES)
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=15,
            transient=True,
        )
        self._handler = _LiveHandler(self)
        self._handler.setLevel(logging.DEBUG)

    # -- public API -----------------------------------------------------------

    def update_spinner(self, text: str) -> None:
        self._spinner_text = text
        self._live.update(self._render())

    def add(self, line: str) -> None:
        self._events.append(line)
        self._live.update(self._render())

    @property
    def handler(self) -> logging.Handler:
        return self._handler

    def start(self) -> None:
        global _display
        _display = self
        self._live.start()

    def stop(self) -> None:
        global _display
        _display = None
        self._live.__exit__(None, None, None)

    def __enter__(self) -> LiveDisplay:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # -- internals ------------------------------------------------------------

    def _render(self) -> Group:
        parts: list = [Spinner("dots", text=self._spinner_text)]
        for msg in self._events:
            parts.append(Text(f"  {msg}", style="dim"))
        return Group(*parts)


class _LiveHandler(logging.Handler):
    def __init__(self, display: LiveDisplay) -> None:
        super().__init__()
        self._display = display

    def emit(self, record: logging.LogRecord) -> None:
        msg = _redact(record.getMessage())
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        pad = " " * (len(ts) + 2)
        for i, line in enumerate(msg.splitlines()):
            prefix = f"{ts}  " if i == 0 else pad
            self._display.add(f"{prefix}{line}")


# Module-level active display
_display: LiveDisplay | None = None


def update_spinner(text: str) -> None:
    """Update the live display spinner text (no-op if no display)."""
    if _display:
        _display.update_spinner(text)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(
    log_dir: str, verbose: bool = False, display: LiveDisplay | None = None
) -> logging.Logger:
    """Configure the ``sunbeam_deployer`` logger.

    Terminal: live display (if provided), else RichHandler.
    File: plain text, full detail.
    """
    log_path = Path(os.path.expanduser(log_dir))
    log_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = log_path / f"sunbeam-deploy-{timestamp}.log"

    logger = logging.getLogger("sunbeam_deployer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # File handler — full detail, plain text
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        _RedactingFormatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        )
    )
    logger.addHandler(fh)

    # Terminal handler
    if display is not None:
        th = display.handler
    else:
        th = _PlainRichHandler(
            console=console,
            show_time=True,
            show_level=False,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
        )
        th.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(th)

    logger.info("Log file: %s", log_file)

    # Save the log file path in a global variable for later use
    global _log_file_path
    _log_file_path = log_file

    logger.info("Sunbeam Deployer v%s", _version())
    return logger


# Re-exported for backward compat
class _PlainRichHandler(RichHandler):
    """RichHandler that redacts secrets before rendering."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        record.msg = _redact(msg)
        record.args = None
        super().emit(record)


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("sunbeam-deployer")
    except Exception:
        return "0.0.0"
