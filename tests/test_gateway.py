from __future__ import annotations

import asyncio
import json
import logging
import sys

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


@pytest.mark.parametrize("code", [4001, 4003, 4004])
def test_auth_close_codes_raise_without_reconnecting(serve, code):
    attempts = 0

    async def run():
        nonlocal attempts

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal attempts
            attempts += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=code, message=b"Unauthorized")
            return ws

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError, match=str(code)):
                await asyncio.wait_for(local_gateway(port).connect(), timeout=10)

    asyncio.run(run())
    assert attempts == 1


def test_ordinary_close_reconnects_then_stops_on_auth_failure(serve):
    attempts = 0

    async def run():
        nonlocal attempts

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal attempts
            attempts += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=1001 if attempts == 1 else 4001, message=b"bye")
            return ws

        async with serve(handler) as port:
            with pytest.raises(AuthenticationError):
                await asyncio.wait_for(local_gateway(port).connect(), timeout=15)

    asyncio.run(run())
    assert attempts == 2


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
