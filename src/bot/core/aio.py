"""Small asyncio helpers."""

from __future__ import annotations

import asyncio
import contextlib


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True early if `stop` was set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()
