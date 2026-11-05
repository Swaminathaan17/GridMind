"""Timestamped, immediately flushed progress reporting for benchmark runs.

Progress is written to stderr by default so it never contaminates
machine-readable stdout or JSON artifacts. Reporting can be disabled with
``ProgressReporter(enabled=False)`` or the ``--quiet`` command-line flag.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import TextIO


def utc_timestamp() -> str:
    """Current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProgressReporter:
    """Write timestamped progress lines to a text stream and flush each line."""

    def __init__(self, *, enabled: bool = True, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr

    def __call__(self, message: str) -> None:
        if not self.enabled:
            return
        self.stream.write(f"[{utc_timestamp()}] {message}\n")
        self.stream.flush()
