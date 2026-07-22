"""Console + file logging for the daily run."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import PROJECT_ROOT

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger("stockbot")
    if _CONFIGURED:
        return root

    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    target = log_file or (PROJECT_ROOT / "data" / "logs" / "daily.log")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(target, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        root.warning("could not open log file %s — console only", target)

    # yfinance is chatty about missing fields we already handle.
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"stockbot.{name}")
