"""Shared alert helper for ops monitors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from bot.core.events import AlertLevel, RiskAlert
from bot.log import get_logger

AlertSink = Callable[[RiskAlert], Awaitable[None]]
_log = get_logger(__name__)


async def emit(
    sink: AlertSink | None, level: AlertLevel, code: str, message: str, **details: object
) -> None:
    log = _log.warning if level is not AlertLevel.INFO else _log.info
    log(code, message=message, **{k: str(v) for k, v in details.items()})
    if sink is not None:
        await sink(
            RiskAlert(
                level=level,
                code=code,
                message=message,
                details={k: str(v) for k, v in details.items()},
            )
        )
