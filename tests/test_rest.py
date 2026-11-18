from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import pytest
from aiohttp import web

from taverns import Client, HTTPError, RESTClient
from taverns._url import path_segment
from taverns.gateway import Gateway
from taverns.rest import MAX_RATE_LIMIT_WAIT, _parse_retry_after, _rate_limit_wait

TOKEN = "tavbot_hidden123"
ROOT = Path(__file__).resolve().parent.parent


def local_rest(port: int) -> RESTClient:
    return RESTClient(TOKEN, api_url=f"http://127.0.0.1:{port}/api/v1", allow_insecure=True)


# ─── Path escaping ───────────────────────────────────────


def test_path_segment_escapes_and_refuses_dot_segments():
    assert path_segment("../x") == "..%2Fx"
    assert path_segment("a?b#c") == "a%3Fb%23c"
    assert path_segment("%2e%2e") == "%252e%252e"
    assert path_segment(42) == "42"
    for bad in ("", ".", "..", None):
        with pytest.raises(ValueError):
            path_segment(bad)


def test_every_path_in_rest_py_is_escaped():
    source = (ROOT / "taverns" / "rest.py").read_text(encoding="utf-8")
    paths = re.findall(r'f"(/[^"]*)"', source)
    assert paths, "no f-string paths found"
    for path in paths:
        for field in re.findall(r"\{([^{}]*)\}", path):
            assert field.startswith("_seg("), f"unescaped {{{field}}} in {path}"


def test_crafted_id_cannot_change_the_route(serve):
    evil = "../evil"
    calls = [
        ("get_channels", (evil,), {}), ("get_members", (evil,), {}),
        ("send_message", ("t", evil), {"content": "x"}), ("edit_message", ("t", evil), {"content": "x"}),
        ("delete_message", ("t", evil), {}), ("get_messages", ("t", evil), {}),
        ("delete_command", (evil,), {}),
        ("reply_to_interaction", (evil,), {"content": "x"}), ("defer_interaction", (evil,), {}),
        ("follow_up_interaction", (evil,), {"content": "x"}),
        ("pin_message", ("t", evil), {}), ("unpin_message", ("t", evil), {}),
        ("get_pinned_messages", ("t", evil), {}), ("add_reaction", ("t", evil, "x"), {}),
        ("remove_reaction", ("t", "m", evil), {}), ("get_member", ("t", evil), {}),
        ("kick_member", ("t", evil), {}), ("ban_member", (evil, "u"), {}),
        ("unban_member", ("t", evil), {}), ("mute_member", (evil, "u"), {}),
        ("unmute_member", ("t", evil), {}), ("search_messages", (evil,), {"query": "q"}),
        ("search_messages", ("t",), {"query": "q", "channel_id": evil}),
        ("get_roles", (evil,), {}), ("join_voice", ("t", evil), {}), ("leave_voice", (evil,), {}),
    ]

    async def run():
        seen: list[str] = []

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            seen.append(f"{request.method} {request.raw_path}")
            return web.Response(status=204)

        async with serve(handler) as port:
            rest = local_rest(port)
            try:
                for name, args, kwargs in calls:
                    seen.clear()
                    try:
                        await getattr(rest, name)(*args, **kwargs)
                    except (TypeError, AttributeError, KeyError):
                        pass  # the empty answer does not parse; only the path matters
                    assert len(seen) == 1, name
                    assert "/api/v1/" in seen[0], (name, seen[0])
                    assert "..%2Fevil" in seen[0], (name, seen[0])
                    assert "/evil" not in seen[0], (name, seen[0])
            finally:
                await rest.close()

    asyncio.run(run())


def test_query_characters_stay_in_the_path_and_dot_ids_are_refused(serve):
    async def run():
        seen: list[str] = []

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            seen.append(f"{request.method} {request.raw_path}")
            return web.Response(status=204)

        async with serve(handler) as port:
            rest = local_rest(port)
            try:
                await rest.unban_member("t", "u?admin=1#x")
                await rest.remove_reaction("t", "m", "\U0001F44D")
                await rest.remove_reaction("t", "m", "a/b")
                with pytest.raises(ValueError):
                    await rest.unban_member("t", "..")
                with pytest.raises(ValueError):
                    await rest.unban_member("t", "")
            finally:
                await rest.close()
        # "?" and "#" stay escaped, so the ID cannot start a query or fragment
        assert len(seen) == 3
        assert seen[0].startswith("DELETE /api/v1/taverns/t/bans/u%3Fadmin")
        assert "?" not in seen[0] and "#" not in seen[0]
        assert unquote(seen[0].rsplit("/", 1)[1]) == "u?admin=1#x"
        assert seen[1] == "DELETE /api/v1/taverns/t/messages/m/reactions/%F0%9F%91%8D"
        assert seen[2] == "DELETE /api/v1/taverns/t/messages/m/reactions/a%2Fb"

    asyncio.run(run())


def test_commands_use_the_bot_routes(serve):
    async def run():
        seen: list[str] = []

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            seen.append(f"{request.method} {request.raw_path}")
            if request.method == "GET":
                return web.json_response([])
            return web.Response(status=204)

        async with serve(handler) as port:
            rest = local_rest(port)
            try:
                assert await rest.get_commands() == []
                await rest.delete_command("cmd-1")
                await rest.register_commands([])
            finally:
                await rest.close()
        assert seen == [
            "GET /api/v1/bots/@me/commands",
            "DELETE /api/v1/bots/@me/commands/cmd-1",
            "PUT /api/v1/bots/@me/commands",
        ]

    asyncio.run(run())


# ─── Rate limits ─────────────────────────────────────────


def test_rate_limit_wait_uses_wall_clock_and_is_capped():
    now = time.time()
    assert _rate_limit_wait(now + 2, now) == pytest.approx(2)
    assert _rate_limit_wait(now - 5, now) == 0
    assert _rate_limit_wait(0, now) == 0
    assert _rate_limit_wait(now + 10**9, now) == MAX_RATE_LIMIT_WAIT
    assert _rate_limit_wait(float("nan"), now) == 0
    assert _rate_limit_wait(float("inf"), now) == 0


def test_retry_after_parsing():
    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after("0.5") == 0.5
    assert _parse_retry_after(None) == 1.0
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 1.0
    assert _parse_retry_after("-5") == 1.0
    assert _parse_retry_after("nan") == 1.0
    assert _parse_retry_after("1e9") == MAX_RATE_LIMIT_WAIT


def test_reset_header_in_epoch_seconds_does_not_hang(serve):
    """X-RateLimit-Reset is Unix time; the next request waits about a second, not decades."""

    async def run():
        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            return web.json_response(
                {"ok": True},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 1)},
            )

        async with serve(handler) as port:
            rest = local_rest(port)
            try:
                await rest.request("GET", "/one")
                started = time.monotonic()
                assert await asyncio.wait_for(rest.request("GET", "/two"), timeout=10) == {"ok": True}
                assert time.monotonic() - started < 5
            finally:
                await rest.close()

    asyncio.run(run())


def test_retry_after_date_and_garbage_headers_do_not_crash(serve):
    async def run():
        calls = 0

        async def handler(request: web.BaseRequest) -> web.StreamResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return web.Response(status=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
            return web.json_response(
                {"ok": True}, headers={"X-RateLimit-Remaining": "abc", "X-RateLimit-Reset": "soon"},
            )

        async with serve(handler) as port:
            rest = local_rest(port)
            try:
                result = await asyncio.wait_for(rest.request("GET", "/x"), timeout=10)
            finally:
                await rest.close()
        assert result == {"ok": True}
        assert calls == 2

    asyncio.run(run())


# ─── Retries ─────────────────────────────────────────────

_JSON = "application/json"
# How the test server answers a request it has fully received
_ANSWERS = {
    "ok": (200, _JSON, b'{"ok":true}'),
    "server_error": (500, _JSON, b'{"message":"Internal server error"}'),
    "empty_server_error": (500, None, b""),
    "bad_gateway": (502, "text/html", b"<html>502 Bad Gateway</html>"),
    "rate_limited": (429, _JSON, b'{"message":"Rate limit exceeded"}'),
}
_FAILURES = ["server_error", "empty_server_error", "bad_gateway", "dropped", "cut_off"]


class RawApi:
    """A plain HTTP/1.1 server on 127.0.0.1 that answers each request by ``plan``.

    ``plan`` lists how to answer each request in turn (the last entry repeats).
    "dropped" closes the connection after the request arrived, before answering;
    "cut_off" closes it in the middle of the answer. Every request is recorded
    once its whole body has arrived.
    """

    def __init__(self, *plan: str) -> None:
        self.plan = list(plan) or ["ok"]
        self.seen: list[tuple[str, str, bytes]] = []
        self.server: asyncio.base_events.Server | None = None

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def reset(self, *plan: str) -> None:
        self.plan = list(plan)
        self.seen = []

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = head.decode("latin-1").split("\r\n")
                method, target, _ = lines[0].split(" ", 2)
                length = 0
                for line in lines[1:]:
                    name, _, value = line.partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value)
                body = await reader.readexactly(length) if length else b""
                self.seen.append((method, target, body))
                answer = self.plan[min(len(self.seen), len(self.plan)) - 1]
                if answer == "dropped":
                    return
                if answer == "cut_off":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: 100\r\n\r\n{\"ok\":")
                    await writer.drain()
                    return
                status, content_type, payload = _ANSWERS[answer]
                headers = f"HTTP/1.1 {status} X\r\nContent-Length: {len(payload)}\r\n"
                if content_type:
                    headers += f"Content-Type: {content_type}\r\n"
                if status == 429:
                    headers += "Retry-After: 0\r\n"
                writer.write(headers.encode() + b"\r\n" + payload)
                await writer.drain()
        finally:
            writer.close()


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retries do not wait in real time; the list records the waits asked for."""
    from taverns import rest as rest_module

    waits: list[float] = []
    real = rest_module._backoff

    def record(retry: int) -> float:
        waits.append(real(retry))
        return 0.0

    monkeypatch.setattr(rest_module, "_backoff", record)
    return waits


# Calls that must never be sent twice once the API may have received them
_ONCE_ONLY = [
    ("POST send_message", lambda r: r.send_message("t", "c", content="hello")),
    ("POST ban_member", lambda r: r.ban_member("t", "u", reason="spam")),
    ("POST mute_member", lambda r: r.mute_member("t", "u", duration_minutes=10)),
    ("POST reply_to_interaction", lambda r: r.reply_to_interaction("i", content="hi")),
    ("POST low-level request", lambda r: r.request("POST", "/custom", json={"a": 1})),
    ("PATCH edit_message", lambda r: r.edit_message("t", "m", content="edited")),
    ("DELETE kick_member", lambda r: r.kick_member("t", "u", reason="spam")),
    ("DELETE unban_member", lambda r: r.unban_member("t", "u")),
    ("DELETE delete_message", lambda r: r.delete_message("t", "m")),
]


def test_post_patch_delete_are_sent_once_when_the_server_fails_after_receiving_them(no_backoff):
    async def run():
        api = RawApi()
        port = await api.start()
        rest = local_rest(port)
        try:
            for name, call in _ONCE_ONLY:
                for failure in _FAILURES:
                    api.reset(failure, "ok")
                    no_backoff.clear()
                    with pytest.raises((HTTPError, aiohttp.ClientError)):
                        await call(rest)
                    await asyncio.sleep(0.02)  # a resend would arrive here
                    assert len(api.seen) == 1, (name, failure, api.seen)
                    assert no_backoff == [], (name, failure)
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


def test_server_errors_raise_http_error_also_with_an_empty_body(no_backoff):
    async def run():
        api = RawApi()
        port = await api.start()
        rest = local_rest(port)
        try:
            api.reset("empty_server_error")
            with pytest.raises(HTTPError) as caught:
                await rest.send_message("t", "c", content="x")
            assert caught.value.status == 500
            api.reset("server_error")
            with pytest.raises(HTTPError, match="Internal server error"):
                await rest.ban_member("t", "u")
            api.reset("bad_gateway")
            with pytest.raises(HTTPError) as caught:
                await rest.unban_member("t", "u")
            assert caught.value.status == 502
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


def test_get_and_put_are_sent_again_after_a_server_error_or_a_lost_connection(no_backoff):
    async def run():
        api = RawApi()
        port = await api.start()
        rest = local_rest(port)
        try:
            for failure in _FAILURES:
                api.reset(failure, "ok")
                no_backoff.clear()
                assert await rest.request("GET", "/x") == {"ok": True}, failure
                assert len(api.seen) == 2, ("GET", failure)
                assert no_backoff == [1.0], failure

                api.reset(failure, "ok")
                commands = [{"name": "ping", "description": "Ping"}]
                assert await rest.request("PUT", "/bots/@me/commands", json=commands) == {"ok": True}
                assert [m for m, _, _ in api.seen] == ["PUT", "PUT"], failure
                assert api.seen[0][2] == api.seen[1][2], "the same body is sent again"
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


def test_retries_back_off_and_stop_after_three(no_backoff):
    async def run():
        api = RawApi("server_error")
        port = await api.start()
        rest = local_rest(port)
        try:
            with pytest.raises(HTTPError) as caught:
                await rest.get_member("t", "u")
            assert caught.value.status == 500
            assert len(api.seen) == 4
            assert no_backoff == [1.0, 2.0, 4.0]
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


def test_a_429_is_sent_again_for_every_method_with_the_same_body(no_backoff):
    async def run():
        api = RawApi()
        port = await api.start()
        rest = local_rest(port)
        try:
            calls = [
                ("POST", lambda: rest.send_message("t", "c", content="hello")),
                ("PATCH", lambda: rest.edit_message("t", "m", content="edited")),
                ("DELETE", lambda: rest.kick_member("t", "u", reason="spam")),
                ("GET", lambda: rest.request("GET", "/x")),
            ]
            for method, call in calls:
                api.reset("rate_limited", "ok")
                try:
                    await call()
                except (TypeError, KeyError):
                    pass  # {"ok": true} is not a message; only the requests matter
                assert [m for m, _, _ in api.seen] == [method, method]
                assert api.seen[0][2] == api.seen[1][2]
            assert no_backoff == []  # waits come from Retry-After, not the backoff
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


def test_a_post_is_sent_again_when_the_connection_was_refused(monkeypatch: pytest.MonkeyPatch):
    from taverns import rest as rest_module

    async def run():
        # Find a free port, then leave it closed so the first connection is refused
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        api = RawApi("ok")
        waits: list[int] = []
        starting: list[asyncio.Task] = []

        def backoff(retry: int) -> float:
            waits.append(retry)
            if not starting:  # the API comes up before the retry
                starting.append(asyncio.get_running_loop().create_task(api.start(port)))
            return 0.3

        monkeypatch.setattr(rest_module, "_backoff", backoff)
        rest = local_rest(port)
        try:
            assert await rest.request("POST", "/custom", json={"a": 1}) == {"ok": True}
            assert waits == [0]
            assert api.seen == [("POST", "/api/v1/custom", b'{"a": 1}')]
        finally:
            await rest.close()
            await api.stop()

    asyncio.run(run())


# ─── Plain http:// and ws:// ─────────────────────────────


def test_plain_http_and_ws_urls_need_allow_insecure():
    with pytest.raises(ValueError, match="allow_insecure"):
        RESTClient(TOKEN, api_url="http://127.0.0.1:1/api/v1")
    with pytest.raises(ValueError, match="allow_insecure"):
        Gateway(TOKEN, ws_url="ws://127.0.0.1:1")
    with pytest.raises(ValueError, match="allow_insecure"):
        Client(api_url="http://127.0.0.1:1/api/v1")
    with pytest.raises(ValueError, match="allow_insecure"):
        Client(ws_url="ws://127.0.0.1:1")
    with pytest.raises(ValueError, match="https://"):
        RESTClient(TOKEN, api_url="ftp://example.com")
    with pytest.raises(ValueError, match="wss://"):
        Gateway(TOKEN, ws_url="https://example.com")
    with pytest.raises(ValueError, match="not a valid URL"):
        RESTClient(TOKEN, api_url="not a url")

    RESTClient(TOKEN)
    RESTClient(TOKEN, api_url="https://example.com/api/v1")
    RESTClient(TOKEN, api_url="http://127.0.0.1:1/api/v1", allow_insecure=True)
    Gateway(TOKEN)
    Gateway(TOKEN, ws_url="ws://127.0.0.1:1", allow_insecure=True)
    Client()
    Client(api_url="http://127.0.0.1:1/api/v1", ws_url="ws://127.0.0.1:1", allow_insecure=True)
