"""structlog JSON logging with secret masking (CLAUDE.md rule 2)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from pydantic import SecretStr

MASK = "***"

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|passphrase|signature|authorization|"
    r"auth_token|proxy_url|database_url|private[_-]?key)",
    re.IGNORECASE,
)
# user:pass@host in URLs (e.g. socks5://user:pass@1.2.3.4:1080)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def mask_text(text: str) -> str:
    """Mask credentials embedded in URLs inside free text."""
    return _URL_CREDENTIALS.sub(rf"\g<scheme>{MASK}@", text)


def _mask_value(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return _mask_mapping(value)
    if isinstance(value, list | tuple | set | frozenset):
        return type(value)(_mask_value(v) for v in value)
    return value


def _mask_mapping(data: Mapping[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY.search(str(key)) and value not in (None, ""):
            out[key] = MASK
        else:
            out[key] = _mask_value(value)
    return out


def mask_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mask sensitive keys and URL credentials."""
    return _mask_mapping(event_dict)


def build_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Render tracebacks BEFORE masking so exception text is masked too.
        structlog.processors.format_exc_info,
        mask_secrets,
        structlog.processors.JSONRenderer(default=str),
    ]


def configure_logging(level: str = "INFO") -> None:
    numeric = logging.getLevelNamesMapping()[level.upper()]
    logging.basicConfig(format="%(message)s", level=numeric)
    structlog.configure(
        processors=build_processors(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
