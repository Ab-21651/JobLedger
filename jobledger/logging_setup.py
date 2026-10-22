"""
Structured logging for JobLedger.
- Logs to console (INFO+ by default).
- Logs to a rotating file in LOG_DIR.
- Never logs secrets.
"""
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

_configured = False


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> logging.Logger:
    global _configured
    if _configured:
        return logging.getLogger("jobledger")

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"jobledger_{datetime.utcnow().strftime('%Y%m%d')}.log"

    level = getattr(logging, log_level.upper(), logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-30s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    root = logging.getLogger("jobledger")
    root.setLevel(level)
    root.propagate = False

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jobledger.{name}")
