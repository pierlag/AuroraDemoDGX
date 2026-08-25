"""Bus d'événements temps réel (SSE) et journal circulaire."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

_MAX_LOGS = 400


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._logs: deque[dict] = deque(maxlen=_MAX_LOGS)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0
        self._closing = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def closing(self) -> bool:
        """Vrai dès que l'arrêt du serveur est demandé : les flux SSE doivent se clore."""
        return self._closing

    def request_shutdown(self) -> None:
        # Appelé depuis un gestionnaire de signal : simple affectation, rien d'asynchrone.
        self._closing = True

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # -- émission ----------------------------------------------------------
    def emit(self, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._seq += 1
        event = {"id": self._seq, "type": kind, "ts": time.time(), "data": payload or {}}
        if kind == "log":
            self._logs.append(event)
        if self._loop is None:
            return
        for q in list(self._subscribers):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except (RuntimeError, asyncio.QueueFull):
                self._subscribers.discard(q)

    def log(self, message: str, level: str = "info", source: str = "system") -> None:
        self.emit("log", {"message": message, "level": level, "source": source})

    def history(self) -> list[dict]:
        return list(self._logs)


bus = EventBus()
