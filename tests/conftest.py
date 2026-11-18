"""Shared test helpers. Every server runs on 127.0.0.1; nothing leaves the machine."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

import pytest
from aiohttp import web

Handler = Callable[[web.BaseRequest], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def _serve(handler: Handler) -> AsyncIterator[int]:
    """Run ``handler`` for every request on a free local port; yields the port."""
    # No access log: it would print the test server's view of each request line
    runner = web.ServerRunner(web.Server(handler, access_log=None))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield port
    finally:
        await runner.cleanup()


@pytest.fixture
def serve() -> Callable[[Handler], object]:
    return _serve
