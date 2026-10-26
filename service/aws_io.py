"""Blocking boto3 calls, run off the event loop on a small dedicated thread pool.

boto3 is synchronous. Calling it directly from a coroutine would freeze the event loop for
every network round trip; asyncio.to_thread would use the shared default pool, which in
Round 1 turned into GIL/thread contention at peak (see store/db.py). A fixed pool of a few
threads keeps AWS I/O concurrent (each call is ~5-20ms of mostly waiting) without letting
the thread count grow with load. boto3 *clients* are thread-safe, so they are shared.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import boto3
from botocore.config import Config

# Standard retry mode backs off on throttling and transient 5xx; tight timeouts so a hung
# AWS call becomes a PersistenceError (answered 500, client retries) instead of a stall.
BOTO_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=2,
    read_timeout=5,
    max_pool_connections=16,
)


class AwsIO:
    def __init__(self, region: str | None, max_workers: int = 8) -> None:
        self._session = boto3.session.Session(region_name=region)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aws")

    def client(self, service: str, read_timeout: float | None = None) -> Any:
        """`read_timeout` overrides the 5 s default for calls that legitimately wait, i.e.
        SQS long polling: a 20 s poll on a 5 s socket timeout fails on every empty queue."""
        config = BOTO_CONFIG
        if read_timeout is not None:
            config = config.merge(Config(read_timeout=read_timeout))
        return self._session.client(service, config=config)

    async def run[T](self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

    def close(self) -> None:
        self._executor.shutdown(wait=True)
