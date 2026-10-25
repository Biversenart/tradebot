"""asyncio.Queue based publish/subscribe bus.

Events are dispatched in publish order. Handlers subscribed to a base class receive all
subclasses (subscribe to `Event` to receive everything). A failing handler never stops the
bus; failures are logged and reported via `on_handler_error` (e.g. to feed the kill switch's
consecutive-error counter).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from bot.core.events import Event
from bot.log import get_logger

E = TypeVar("E", bound=Event)
Handler = Callable[[E], Awaitable[None]]
ErrorCallback = Callable[[Event, BaseException], None]

_log = get_logger(__name__)


class EventBusFullError(RuntimeError):
    """Raised by `publish_nowait` when the queue is full."""


class EventBusClosedError(RuntimeError):
    """Raised when publishing to a stopped bus."""


_STOP = object()


class EventBus:
    def __init__(
        self, maxsize: int = 10_000, on_handler_error: ErrorCallback | None = None
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize pozitif olmalı (sınırsız kuyruk kabul edilmez).")
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self._handlers: defaultdict[type[Event], list[Handler[Any]]] = defaultdict(list)
        self._on_handler_error = on_handler_error
        self._closed = False
        self._running = False
        self.handler_errors = 0
        self.dispatched = 0

    # ------------------------------------------------------------------ subscription
    def subscribe(self, event_type: type[E], handler: Handler[E]) -> None:
        handlers = self._handlers[event_type]
        if handler not in handlers:
            handlers.append(cast(Handler[Any], handler))

    def unsubscribe(self, event_type: type[E], handler: Handler[E]) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(cast(Handler[Any], handler))

    def handlers_for(self, event_type: type[Event]) -> list[Handler[Any]]:
        result: list[Handler[Any]] = []
        for klass in event_type.__mro__:
            if isinstance(klass, type) and issubclass(klass, Event):
                for h in self._handlers.get(klass, []):
                    if h not in result:
                        result.append(h)
        return result

    # ------------------------------------------------------------------ publishing
    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def publish(self, event: Event) -> None:
        """Enqueue an event, waiting if the queue is full (backpressure)."""
        self._ensure_open()
        await self._queue.put(event)

    def publish_nowait(self, event: Event) -> None:
        self._ensure_open()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise EventBusFullError("EventBus kuyruğu dolu.") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise EventBusClosedError("EventBus durduruldu; yeni olay kabul edilmiyor.")

    # ------------------------------------------------------------------ dispatch loop
    async def run(self) -> None:
        """Dispatch events until `stop()` is called. Events queued before stop are drained."""
        if self._running:
            raise RuntimeError("EventBus zaten çalışıyor.")
        self._running = True
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _STOP:
                        return
                    await self._dispatch(cast(Event, item))
                finally:
                    self._queue.task_done()
        finally:
            self._running = False

    async def stop(self) -> None:
        """Stop accepting events; `run()` returns after draining already-queued events."""
        if self._closed:
            return
        self._closed = True
        await self._queue.put(_STOP)

    async def _dispatch(self, event: Event) -> None:
        for handler in self.handlers_for(type(event)):
            try:
                await handler(event)
            except Exception as exc:
                self.handler_errors += 1
                _log.exception(
                    "event_handler_failed",
                    event_type=type(event).__name__,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                )
                if self._on_handler_error is not None:
                    self._on_handler_error(event, exc)
        self.dispatched += 1
