from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

import pytest
from aiohttp import WSMsgType, web

from taverns import AuthenticationError
from taverns.gateway import Gateway, RedactTokenFilter, redact_token

TOKEN = "tavbot_hidden123"
SECRET_PART = "hidden123"


def local_gateway(port: int, **kwargs) -> Gateway:
    return Gateway(TOKEN, ws_url=f"ws://127.0.0.1:{port}", allow_insecure=True, **kwargs)


# ─── The token never reaches the logs ────────────────────


def test_redact_token_helper():
    assert redact_token("GET /?token=tavbot_abc&intents=3 HTTP/1.1") == "GET /?token=<redacted>&intents=3 HTTP/1.1"
    assert redact_token("wss://ws.tav.gg/?TOKEN=abc") == "wss://ws.tav.gg/?TOKEN=<redacted>"
    assert redact_token("nothing here") == "nothing here"


def test_filter_redacts_arguments_and_exception_text():
    record = logging.LogRecord("x", logging.DEBUG, __file__, 1, "> GET %s HTTP/1.1", ("/?token=" + TOKEN,), None)
    RedactTokenFilter().filter(record)
    assert SECRET_PART not in record.getMessage()
    assert "token=<redacted>" in record.getMessage()

    try:
        raise RuntimeError("could not open wss://example.com/?token=" + TOKEN)
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), exc_info)
    RedactTokenFilter().filter(record)
    assert SECRET_PART not in logging.Formatter().format(record)


def test_debug_logs_never_contain_the_token(serve, caplog):
    caplog.set_level(logging.DEBUG)
    received: list[str] = []

    async def run():
        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            received.append(request.query.get("token", ""))
            return web.Response(status=401, text="Unauthorized")

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError) as excinfo:
                await asyncio.wait_for(local_gateway(port, intents=3).connect(), timeout=10)
        return excinfo.value

    error = asyncio.run(run())
    assert received == [TOKEN], "the handshake carries the token"
    client_records = [r for r in caplog.records if not r.name.startswith("aiohttp")]
    messages = [record.getMessage() for record in client_records]
    assert any("token=<redacted>" in m for m in messages), "the request line was logged and redacted"
    assert not any(SECRET_PART in m for m in messages)
    assert SECRET_PART not in caplog.text
    assert SECRET_PART not in str(error)


# ─── Authentication failures stop reconnecting ───────────


@pytest.mark.parametrize("status", [401, 403])
def test_refused_handshake_raises_without_reconnecting(serve, status):
    attempts = 0

    async def run():
        nonlocal attempts

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal attempts
            attempts += 1
            return web.Response(status=status)

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError, match=f"HTTP {status}"):
                await asyncio.wait_for(local_gateway(port).connect(), timeout=10)

    asyncio.run(run())
    assert attempts == 1


@pytest.fixture
def instant_reconnect(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Reconnects happen at once; the list records the close code before each one."""
    codes: list[int | None] = []
    real = Gateway._reconnect_delay

    def record(self: Gateway) -> float:
        codes.append(self._close_code)
        real(self)
        return 0.0

    monkeypatch.setattr(Gateway, "_reconnect_delay", record)
    return codes


# The gateway closes with these (age limit, no answer to pings, too many frames
# or a ban, a frame too large, a restart). None of them means a refused token:
# the token is refused only when connecting, with HTTP 401 or 403.
@pytest.mark.parametrize("code", [1000, 1001, 1008, 1009, 1012, 4001])
def test_every_close_reconnects_and_a_refused_token_on_reconnect_stops(serve, instant_reconnect, code):
    attempts = 0

    async def run():
        nonlocal attempts

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                return web.Response(status=401, text="Unauthorized")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=code, message=b"bye")
            return ws

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError, match="HTTP 401"):
                await asyncio.wait_for(local_gateway(port).connect(), timeout=10)

    asyncio.run(run())
    assert attempts == 2
    assert instant_reconnect == [code]


def test_reconnect_delay_follows_the_close_code(monkeypatch: pytest.MonkeyPatch):
    from taverns import gateway as gateway_module

    monkeypatch.setattr(gateway_module.random, "uniform", lambda a, b: b)  # the longest jitter
    gateway = Gateway(TOKEN)

    def after(code: int | None, lasted: float | None) -> float:
        gateway._close_code = code
        gateway._opened_at = None if lasted is None else time.monotonic() - lasted
        return gateway._reconnect_delay()

    # The age limit (1000) and a restart (1012): back at once, the backoff does not grow
    assert after(1000, 5) <= 1
    assert after(1012, 5) <= 1
    assert after(1012, 5) <= 1
    # Any other close, or a connection that never opened: 1, 2, 4, 8 seconds (+ jitter), at most 30
    gateway._reconnect_attempt = 0
    assert [after(1008, 1), after(1001, 1), after(None, None), after(1006, 1)] == [2, 3, 5, 9]
    for _ in range(5):
        after(None, None)
    assert after(None, None) == gateway_module.MAX_RECONNECT_DELAY
    # A connection that stayed up a while starts the backoff over
    assert after(1001, gateway_module.STABLE_CONNECTION) == 2


# ─── Heartbeat and ping ──────────────────────────────────


def test_heartbeat_and_ping_are_sent_and_pong_is_not_an_event(serve, monkeypatch: pytest.MonkeyPatch):
    from taverns import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "HEARTBEAT_INTERVAL", 0.05)
    received: list[dict] = []
    events: list[str] = []

    async def run():
        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                frame = json.loads(msg.data)
                received.append(frame)
                if frame["action"] == "ping":
                    await ws.send_str(json.dumps({"event": "pong", "data": {"t": 1}}))
                    if len(received) == 2:
                        await ws.send_str(json.dumps({"event": "tavern_message", "data": {"id": "m1"}}))
            return ws

        async with serve(handler) as port:
            gateway = None

            async def on_event(name: str, data: dict) -> None:
                events.append(name)
                assert gateway is not None
                await gateway.disconnect()

            gateway = local_gateway(port, on_event=on_event)
            await asyncio.wait_for(gateway.connect(), timeout=10)

    asyncio.run(run())
    assert received[:2] == [{"action": "heartbeat", "data": {}}, {"action": "ping"}]
    assert events == ["message_create"]


def test_a_silent_gateway_is_dropped_and_connected_again(serve, monkeypatch: pytest.MonkeyPatch, instant_reconnect):
    from taverns import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(gateway_module, "PONG_TIMEOUT", 0.2)
    attempts = 0

    async def run():
        nonlocal attempts

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                return web.Response(status=403)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for _ in ws:
                pass  # reads every ping, never answers
            return ws

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError, match="HTTP 403"):
                await asyncio.wait_for(local_gateway(port).connect(), timeout=10)

    asyncio.run(run())
    assert attempts == 2
    assert len(instant_reconnect) == 1


def test_an_event_that_fails_to_handle_does_not_drop_the_connection(serve):
    events: list[str] = []
    connections = 0

    async def run():
        nonlocal connections

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal connections
            connections += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("not json")
            await ws.send_str(json.dumps({"event": "tavern_channel_updated", "data": {"id": "c1"}}))
            await ws.send_str(json.dumps({"event": "tavern_message", "data": None}))
            await ws.receive()  # wait for the client to close
            return ws

        async with serve(handler) as port:
            gateway = None

            async def on_event(name: str, data: dict) -> None:
                events.append(name)
                if name == "channel_update":
                    raise KeyError("name")
                assert data == {}
                assert gateway is not None
                await gateway.disconnect()

            gateway = local_gateway(port, on_event=on_event)
            await asyncio.wait_for(gateway.connect(), timeout=10)

    asyncio.run(run())
    assert events == ["channel_update", "message_create"]
    assert connections == 1


# ─── websockets 14+ connection state ─────────────────────


def test_is_connected_and_send_work_on_current_websockets(serve):
    sent: list[dict] = []
    states: list[bool] = []

    async def run():
        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(json.dumps({"event": "tavern_message", "data": {"id": "m1"}}))
            msg = await ws.receive()
            if msg.type == WSMsgType.TEXT:
                sent.append(json.loads(msg.data))
            await ws.receive()  # wait for the client to close
            return ws

        async with serve(handler) as port:
            gateway = None

            async def on_event(name: str, data: dict) -> None:
                assert gateway is not None
                states.append(gateway.is_connected)
                await gateway.send("tavern_typing_start", {"tavernId": "t", "channelId": "c"})
                await gateway.disconnect()
                states.append(gateway.is_connected)

            gateway = local_gateway(port, on_event=on_event)
            assert gateway.is_connected is False
            await asyncio.wait_for(gateway.connect(), timeout=10)

    asyncio.run(run())
    assert states == [True, False]
    assert sent == [{"action": "tavern_typing_start", "data": {"tavernId": "t", "channelId": "c"}}]
