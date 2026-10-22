"""Console + rotating-file logging. Never logs secrets or full email bodies."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from jobledger.timeutil import utcnow

_configured = False


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    global _configured
    root = logging.getLogger("jobledger")
    if _configured:
        return root

    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
                                  datefmt="%Y-%m-%dT%H:%M:%S")
    root.setLevel(level)
    root.propagate = False

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_file = Path(log_dir) / f"jobledger_{utcnow().strftime('%Y%m%d')}.log"
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024,
                                                  backupCount=3, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except OSError as exc:  # read-only dir etc.: console logging still works
        root.warning("file logging disabled: %s", exc)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jobledger.{name}")
