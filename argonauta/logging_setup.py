"""File logger for .argonauta/argonauta.log.

Log lines only ever carry ids, paths, counts and short status words. A
redacting filter is still installed as a last line of defence so an accidental
secret-shaped string never lands on disk.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from argonauta.config import SECRET_ENV_VARS

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
LOGGER_NAME = "argonauta"


class RedactingFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = [v for name in SECRET_ENV_VARS if (v := os.environ.get(name, "").strip())]

    def _scrub(self, text: str) -> str:
        text = _KEY_PATTERN.sub("[redacted]", text)
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(str(record.msg))
        if record.args:
            record.args = tuple(self._scrub(str(a)) for a in record.args)
        return True


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(log_path: Path | None) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_argonauta_configured", False):
        return logger
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(log_path, maxBytes=512_000, backupCount=2, encoding="utf-8")
    else:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger._argonauta_configured = True  # type: ignore[attr-defined]
    return logger
