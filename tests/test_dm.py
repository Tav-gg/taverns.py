"""Direct message tests. Every server runs on 127.0.0.1; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from aiohttp import web

from taverns import (
    Client,
    DmChannel,
    DmMessage,
    DmMessageDelete,
    DMErrorCode,
    Embed,
    HTTPError,
    Intents,
    RateLimitError,
    RESTClient,
)
from taverns.gateway import EVENT_NAME_MAP
from taverns.rest import MAX_RETRIES

TOKEN = "tavbot_hidden123"
PERSON = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-3333-4444-8555-666666666666"
BOT_USER = "0b0b0b0b-1111-4222-8333-444444444444"


def dm_channel(channel_id: str, user_id: str) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": "dm",
        "recipient": {"id": user_id, "displayName": "Ana", "avatarUrl": None},
        "createdAt": "2026-11-18T00:00:00.000Z",
        "lastMessageAt": None,
    }


def dm_message(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "m1",
        "channelId": "dm-1",
        "content": "hi",
        "filteredContent": None,
        "status": "SENT",
        "replyToId": None,
        "replyTo": None,
        "embeds": [],
        "attachments": [],
        "createdAt": "2026-11-18T00:00:00.000Z",
        "editedAt": None,
        "sender": {"id": PERSON, "displayName": "Ana", "avatarUrl": None, "isBot": False},
    }
    data.update(overrides)
    return data


class Api:
    """Records every request as (method, path with query, JSON body) and answers with ``answer``."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.seen: list[tuple[str, str, Any]] = []

    async def handler(self, request: web.BaseRequest) -> web.StreamResponse:
        raw = await request.read()
        body = await request.json() if raw else None
        self.seen.append((request.method, request.raw_path, body))
        status, payload, headers = self.answer(request.method, request.raw_path, body)
        return web.json_response(payload, status=status, headers=headers or {})

    def routes(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.seen]


def dm_api():
    """A tiny API for the DM routes: opens dm-<n> per person and echoes sends."""
    channels: dict[str, str] = {}

    def answer(method: str, path: str, body: Any):
        if method == "POST" and path == "/api/v1/bots/@me/dms":
            channels.setdefault(body["userId"], f"dm-{len(channels) + 1}")
            return 200, dm_channel(channels[body["userId"]], body["userId"]), None
        sent = re.fullmatch(r"/api/v1/bots/@me/dms/([^/]+)/messages", path)
        if method == "POST" and sent:
            bot = {"id": BOT_USER, "displayName": "Bot", "avatarUrl": None, "isBot": True}
            return 201, dm_message(id="sent", channelId=sent.group(1), content=body.get("content", ""), sender=bot), None
        return 200, {}, None

    return answer


def run_with(api: Api, serve, test) -> None:
    """Run ``test(client)`` against ``api``; the client is logged in by hand (no gateway)."""

    async def run():
        async with serve(api.handler) as port:
            url = f"http://127.0.0.1:{port}/api/v1"
            client = Client(api_url=url, allow_insecure=True)
            client._rest = RESTClient(TOKEN, api_url=url, allow_insecure=True)
            try:
                await test(client)
            finally:
                await client._rest.close()

    asyncio.run(run())


# ─── Intents ─────────────────────────────────────────────


def test_direct_messages_is_bit_16_and_in_default_and_all():
    assert Intents.DIRECT_MESSAGES == 65536
    assert Intents.DEFAULT == 0x17FF9 == 98297
    assert Intents.ALL == 0x1FFFF == 131071
    assert Intents.DEFAULT & Intents.DIRECT_MESSAGES
    assert not Intents.DEFAULT & Intents.MESSAGE_CONTENT
    assert not Intents.DEFAULT & Intents.MEMBERS
    assert Intents.TYPING == 4096  # bit 12 stays typing
    assert Intents.from_list("direct_messages", "messages") == (1 << 16) | 1


# ─── REST routes ─────────────────────────────────────────


def test_create_dm_opens_the_dm(serve):
    api = Api(dm_api())

    async def test(client: Client):
        channel = await client.rest.create_dm(PERSON)
        assert isinstance(channel, DmChannel)
        assert channel.id == "dm-1"
        assert channel.recipient.id == PERSON
        assert channel.type == "dm"
        assert api.seen == [("POST", "/api/v1/bots/@me/dms", {"userId": PERSON})]

    run_with(api, serve, test)


def test_send_dm_opens_once_per_person_then_reuses_the_channel(serve):
    api = Api(dm_api())

    async def test(client: Client):
        first = await client.send_dm(PERSON, "hello")
        assert isinstance(first, DmMessage)
        assert first.channel_id == "dm-1"
        assert first.sender.is_bot
        assert api.routes() == ["POST /api/v1/bots/@me/dms", "POST /api/v1/bots/@me/dms/dm-1/messages"]
        assert api.seen[1][2] == {"content": "hello"}

        api.seen.clear()
        await client.send_dm(PERSON, "again", embeds=[Embed(title="Stats")], reply_to_id="m1", nonce="n-1")
        assert api.routes() == ["POST /api/v1/bots/@me/dms/dm-1/messages"]
        assert api.seen[0][2] == {
            "content": "again", "embeds": [{"title": "Stats"}], "replyToId": "m1", "nonce": "n-1",
        }

        api.seen.clear()
        await client.send_dm(OTHER, "hi there")
        assert api.routes() == ["POST /api/v1/bots/@me/dms", "POST /api/v1/bots/@me/dms/dm-2/messages"]

    run_with(api, serve, test)


def test_two_sends_at_once_open_the_dm_once(serve):
    api = Api(dm_api())

    async def test(client: Client):
        await asyncio.gather(client.send_dm(PERSON, "a"), client.send_dm(PERSON, "b"))
        assert api.routes().count("POST /api/v1/bots/@me/dms") == 1
        assert api.routes().count("POST /api/v1/bots/@me/dms/dm-1/messages") == 2

    run_with(api, serve, test)


def test_send_dm_opens_again_when_the_remembered_channel_is_gone(serve):
    inner = dm_api()
    gone = {"now": False}

    def answer(method: str, path: str, body: Any):
        if gone["now"] and method == "POST" and path == "/api/v1/bots/@me/dms/dm-1/messages":
            gone["now"] = False
            return 404, {"statusCode": 404, "code": "DM_CHANNEL_NOT_FOUND", "error": "DM_CHANNEL_NOT_FOUND",
                         "message": "Direct message not found"}, None
        return inner(method, path, body)

    api = Api(answer)

    async def test(client: Client):
        await client.send_dm(PERSON, "one")
        gone["now"] = True
        api.seen.clear()
        sent = await client.send_dm(PERSON, "two")
        assert sent.content == "two"
        assert api.routes() == [
            "POST /api/v1/bots/@me/dms/dm-1/messages",
            "POST /api/v1/bots/@me/dms",
            "POST /api/v1/bots/@me/dms/dm-1/messages",
        ]

    run_with(api, serve, test)


def test_get_edit_and_delete_use_the_dm_routes(serve):
    def answer(method: str, path: str, body: Any):
        if method == "GET" and path == "/api/v1/bots/@me/dms/dm-1":
            return 200, dm_channel("dm-1", PERSON), None
        if method == "GET":
            return 200, {"messages": [dm_message()], "hasMore": True}, None
        if method == "PATCH":
            return 200, dm_message(id="m9", content="edited"), None
        return 200, {"deleted": True}, None

    api = Api(answer)

    async def test(client: Client):
        channel = await client.get_dm_channel("dm-1")
        assert channel.recipient.id == PERSON
        page = await client.get_dm_messages("dm-1", before="m5", limit=20)
        assert page.has_more is True
        assert page.messages[0].id == "m1"
        assert page.messages[0].sender_id == PERSON
        edited = await client.edit_dm_message("dm-1", "m9", "edited")
        assert edited.content == "edited"
        await client.rest.edit_dm_message("dm-1", "m9", embeds=[Embed(title="New")])
        assert await client.delete_dm_message("dm-1", "m9") is None
        assert api.routes() == [
            "GET /api/v1/bots/@me/dms/dm-1",
            "GET /api/v1/bots/@me/dms/dm-1/messages?before=m5&limit=20",
            "PATCH /api/v1/bots/@me/dms/dm-1/messages/m9",
            "PATCH /api/v1/bots/@me/dms/dm-1/messages/m9",
            "DELETE /api/v1/bots/@me/dms/dm-1/messages/m9",
        ]
        assert api.seen[2][2] == {"content": "edited"}
        assert api.seen[3][2] == {"embeds": [{"title": "New"}]}
        # get_dm_channel remembered the person's channel
        api.seen.clear()
        await client.send_dm(PERSON, "x")
        assert api.routes() == ["POST /api/v1/bots/@me/dms/dm-1/messages"]

    run_with(api, serve, test)


def test_a_crafted_id_cannot_change_the_route_of_a_dm_helper(serve):
    api = Api(lambda method, path, body: (200, {"id": "x", "messages": []}, None))
    evil = "../evil"

    async def test(client: Client):
        rest = client.rest
        calls = [
            rest.get_dm_channel(evil), rest.get_dm_messages(evil), rest.send_dm_message(evil, "x"),
            rest.edit_dm_message(evil, "m", "x"), rest.edit_dm_message("c", evil, "x"),
            rest.delete_dm_message(evil, "m"), rest.delete_dm_message("c", evil),
        ]
        for call in calls:
            await call
        assert len(api.seen) == 7
        for _, path, _ in api.seen:
            assert "..%2Fevil" in path and "/evil" not in path, path

    run_with(api, serve, test)


# ─── Errors ──────────────────────────────────────────────


def test_a_dm_limit_raises_at_once_with_scope_and_retry_after(serve):
    body = {
        "statusCode": 429, "code": "BOT_DM_RATE_LIMITED", "error": "BOT_DM_RATE_LIMITED",
        "message": "You are sending direct messages too fast.", "scope": "unanswered", "retryAfter": 3600,
    }
    api = Api(lambda method, path, _body: (429, body, {"Retry-After": "3600"}))

    async def test(client: Client):
        with pytest.raises(RateLimitError) as excinfo:
            await asyncio.wait_for(client.send_dm_message("dm-1", "x"), timeout=5)
        error = excinfo.value
        assert isinstance(error, HTTPError)
        assert error.status == 429
        assert error.code == DMErrorCode.BOT_DM_RATE_LIMITED
        assert error.scope == "unanswered"
        assert error.retry_after == 3600  # the real wait, not capped
        assert "too fast" in str(error)
        assert len(api.seen) == 1, "not sent again"

    run_with(api, serve, test)


def test_a_dm_limit_without_a_body_retry_after_takes_the_header(serve):
    body = {"statusCode": 429, "code": "BOT_DM_RATE_LIMITED", "message": "Slow down", "scope": "open_burst"}
    api = Api(lambda method, path, _body: (429, body, {"Retry-After": "5"}))

    async def test(client: Client):
        with pytest.raises(RateLimitError) as excinfo:
            await asyncio.wait_for(client.send_dm(PERSON, "x"), timeout=5)
        assert excinfo.value.scope == "open_burst"
        assert excinfo.value.retry_after == 5
        assert len(api.seen) == 1

    run_with(api, serve, test)


def test_the_general_rate_limit_is_still_retried_then_raises(serve):
    body = {"statusCode": 429, "message": "Rate limit exceeded", "error": "Too Many Requests"}
    api = Api(lambda method, path, _body: (429, body, {"Retry-After": "0"}))

    async def test(client: Client):
        with pytest.raises(RateLimitError) as excinfo:
            await asyncio.wait_for(client.send_dm_message("dm-1", "x"), timeout=5)
        assert excinfo.value.scope is None
        assert excinfo.value.code is None  # "Too Many Requests" is not an error code
        assert len(api.seen) == MAX_RETRIES + 1

    run_with(api, serve, test)


def test_403_cannot_dm_user_reaches_the_caller_with_its_code(serve):
    body = {"statusCode": 403, "code": "CANNOT_DM_USER", "error": "CANNOT_DM_USER",
            "message": "Cannot send messages to this user"}
    api = Api(lambda method, path, _body: (403, body, None))

    async def test(client: Client):
        for _ in range(2):
            with pytest.raises(HTTPError) as excinfo:
                await client.send_dm(PERSON, "hi")
            error = excinfo.value
            assert not isinstance(error, RateLimitError)
            assert error.status == 403
            assert error.code == DMErrorCode.CANNOT_DM_USER
            assert "Cannot send messages to this user" in str(error)
        # Nothing was remembered: each send tries to open the DM again
        assert api.routes() == ["POST /api/v1/bots/@me/dms", "POST /api/v1/bots/@me/dms"]

    run_with(api, serve, test)


def test_error_codes_come_from_code_or_an_error_that_is_a_code():
    assert HTTPError(403, "x", response_body={"code": "BOT_DM_DISABLED", "error": "Forbidden"}).code == "BOT_DM_DISABLED"
    assert HTTPError(403, "x", response_body={"error": "BOT_DM_REVIEW_REQUIRED"}).code == "BOT_DM_REVIEW_REQUIRED"
    assert HTTPError(403, "x", response_body={"error": "Forbidden"}).code is None
    assert HTTPError(500, "x").code is None


# ─── Gateway events ──────────────────────────────────────


def test_dm_events_are_mapped_and_typed(serve):
    assert EVENT_NAME_MAP["dm_message_create"] == "dm_message_create"
    assert EVENT_NAME_MAP["dm_message_update"] == "dm_message_update"
    assert EVENT_NAME_MAP["dm_message_delete"] == "dm_message_delete"
    api = Api(dm_api())

    async def test(client: Client):
        received: list[tuple[str, Any]] = []

        @client.event
        async def on_dm_message_create(message):
            received.append(("create", message))

        @client.event
        async def on_dm_message_update(message):
            received.append(("update", message))

        @client.on("dm_message_delete")
        async def deleted(event):
            received.append(("delete", event))

        await client._handle_event("dm_message_create", dm_message(id="m1", channelId="dm-7", content="help"))
        await client._handle_event("dm_message_update", dm_message(id="m1", channelId="dm-7", content="edited",
                                                                    editedAt="2026-11-18T00:01:00.000Z"))
        await client._handle_event("dm_message_delete", {"id": "m1", "channelId": "dm-7"})

        kinds = [kind for kind, _ in received]
        assert kinds == ["create", "update", "delete"]
        created = received[0][1]
        assert isinstance(created, DmMessage)
        assert (created.id, created.channel_id, created.content, created.sender_id) == ("m1", "dm-7", "help", PERSON)
        assert received[1][1].edited_at == "2026-11-18T00:01:00.000Z"
        assert received[2][1] == DmMessageDelete(id="m1", channel_id="dm-7")

        # A reply goes to the same DM
        await client.send_dm_message(created.channel_id, "On it!", reply_to_id=created.id)
        assert api.seen[-1] == ("POST", "/api/v1/bots/@me/dms/dm-7/messages", {"content": "On it!", "replyToId": "m1"})

        # The person's channel is known now: send_dm does not open it again
        api.seen.clear()
        await client.send_dm(PERSON, "later")
        assert api.routes() == ["POST /api/v1/bots/@me/dms/dm-7/messages"]

    run_with(api, serve, test)
