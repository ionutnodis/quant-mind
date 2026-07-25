"""ConnectionManager (Engineering Constraint 1).

One long-lived connection owner. IB Gateway restarts nightly and rejects rapid
reconnects with clientId conflicts, so: fixed clientId, exponential backoff with
a cap, and `ensure_connected()` as the health probe every scheduled job calls
before touching the broker.

    ensure_connected()
        │ isConnected? ──yes──> return
        ▼ no
    connectAsync(host, port, clientId)
        │ ok ──> return
        ▼ fail
    sleep(min(base * 2^attempt, max)) ──> retry ──> ... ──> ConnectionError after max_attempts
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class ConnectionManager:
    def __init__(
        self,
        ib,
        host: str,
        port: int,
        client_id: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        max_attempts: int = 10,
    ):
        self._ib = ib
        self._host = host
        self._port = port
        self._client_id = client_id  # fixed: never rotated, never randomized
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._max_attempts = max_attempts

    async def ensure_connected(self) -> None:
        """Health probe + reconnect. Call before every scheduled job."""
        if self._ib.isConnected():
            return
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)
                return
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < self._max_attempts - 1:
                    delay = min(self._backoff_base * (2**attempt), self._backoff_max)
                    await self._sleep(delay)
        raise ConnectionError(
            f"could not connect to IB Gateway at {self._host}:{self._port} "
            f"after {self._max_attempts} attempts"
        ) from last_error
