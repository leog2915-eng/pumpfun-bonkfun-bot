"""Log setup. Endpoint secrets and API keys are masked before any handler runs."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from utils.logger import install_secret_redaction, register_secret


def configure_logging(log_path: Path) -> None:
    """Attach one file handler. Idempotent for that path."""
    install_secret_redaction()
    for name in ("GMGN_API_KEY", "GMGN_PRIVATE_KEY"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            register_secret(value)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    path = Path(log_path).resolve()
    target = str(path)
    if any(
        isinstance(handler, logging.FileHandler) and handler.baseFilename == target
        for handler in root.handlers
    ):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(handler)
