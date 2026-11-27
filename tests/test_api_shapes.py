"""The REST helpers send what the API reads and read what it answers.
Every server runs on 127.0.0.1; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from taverns import Embed, Member, Message, MessagePage, Permissions, RESTClient
from taverns.permissions import KICK_MEMBERS, SEND_MESSAGES

TOKEN = "tavbot_hidden123"
TAVERN = "7a7a7a7a-1111-4222-8333-444444444444"
HALL = "c0c0c0c0-1111-4222-8333-444444444444"
PERSON = "11111111-2222-4333-8444-555555555555"
ROLE = "a0a0a0a0-1111-4222-8333-444444444444"


def member_row(**overrides: Any) -> dict[str, Any]:
    """A member as GET /taverns/:id/members lists it."""
    row: dict[str, Any] = {
        "id": "row-1",
        "userId": PERSON,
        "nickname": None,
        "joinedAt": "2026-11-20T10:00:00.000Z",
        "user": {"id": PERSON, "displayName": "Ana", "avatarUrl": "https://cdn.tav.gg/a.png", "username": "ana"},
        "roles": [{"id": ROLE, "name": "Crew", "color": "#FF5733", "position": 3}],
        "isOnline": True,
        "status": "IDLE",
        "isOwner": False,
        "isBot": False,
        "activity": None,
    }
    row.update(overrides)
    return row


def hall_message(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "m1",
        "channelId": HALL,
        "content": "hello",
        "status": "SENT",
        "metadata": None,
        "sender": {"id": PERSON, "displayName": "Ana", "isBot": False},
    }
    data.update(overrides)
    return data


class Recorder:
    """Answers each request from ``answers`` (by "METHOD /path" after /api/v1) and records it."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.seen: list[dict[str, Any]] = []

    async def handler(self, request: web.BaseRequest) -> web.StreamResponse:
        raw = await request.read()
        key = f"{request.method} {request.path.removeprefix('/api/v1')}"
        self.seen.append({
            "key": key,
            "query": dict(request.query),
            "body": json.loads(raw) if raw else None,
        })
        if key in self.answers:
            return web.json_response(self.answers[key])
        return web.Response(status=204)


def run_with(answers: dict[str, Any], calls) -> Recorder:
    recorder = Recorder(answers)

    async def run(serve):
        async with serve(recorder.handler) as port:
            rest = RESTClient(TOKEN, api_url=f"http://127.0.0.1:{port}/api/v1", allow_insecure=True)
            try:
                await calls(rest)
            finally:
                await rest.close()

    return recorder, run


# ─── Halls ───────────────────────────────────────────────


def test_get_channels_reads_the_channels_of_the_answer(serve):
    answers = {
        f"GET /taverns/{TAVERN}/channels": {
            "channels": [
                {"id": HALL, "name": "general", "type": "TEXT", "tavernId": TAVERN, "position": 0,
                 "topic": None, "categoryId": "cat-1"},
                {"id": "voice-1", "name": "Lounge", "type": "VOICE", "position": 1},
            ],
            "categories": [{"id": "cat-1", "name": "Text"}],
        },
    }
    result: list = []

    async def calls(rest: RESTClient) -> None:
        result.extend(await rest.get_channels(TAVERN))

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [(c.id, c.name, c.type, c.tavern_id) for c in result] == [
        (HALL, "general", "TEXT", TAVERN),
        ("voice-1", "Lounge", "VOICE", TAVERN),  # the tavern is filled in when a row leaves it out
    ]
    assert result[0].category_id == "cat-1"


# ─── Messages ────────────────────────────────────────────


def test_get_messages_returns_a_page_and_pages_with_before(serve):
    answers = {
        f"GET /taverns/{TAVERN}/channels/{HALL}/messages": {
            "messages": [hall_message(id="m1"), hall_message(id="m2", content="")],
            "hasMore": True,
            "hasMoreBefore": True,
            "hasMoreAfter": False,
        },
    }
    pages: list[MessagePage] = []

    async def calls(rest: RESTClient) -> None:
        pages.append(await rest.get_messages(TAVERN, HALL))
        pages.append(await rest.get_messages(TAVERN, HALL, limit=10, before="m1"))

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    page = pages[0]
    assert isinstance(page, MessagePage)
    assert page.has_more is True
    assert [m.id for m in page.messages] == ["m1", "m2"]
    assert all(isinstance(m, Message) and m.tavern_id == TAVERN for m in page.messages)
    assert page.messages[1].content == ""
    assert recorder.seen[0]["query"] == {"limit": "50"}
    assert recorder.seen[1]["query"] == {"limit": "10", "before": "m1"}


def test_edit_message_sends_only_the_text_and_returns_the_message(serve):
    answers = {f"PATCH /taverns/{TAVERN}/messages/m1": hall_message(content="edited")}
    edited: list[Message] = []

    async def calls(rest: RESTClient) -> None:
        edited.append(await rest.edit_message(TAVERN, "m1", content="edited"))
        with pytest.raises(TypeError):
            await rest.edit_message(TAVERN, "m1", content="x", embeds=[Embed(title="t")])  # type: ignore[call-arg]

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [s["body"] for s in recorder.seen] == [{"content": "edited"}]
    assert edited[0].content == "edited"
    assert edited[0].tavern_id == TAVERN


def test_send_message_and_pins_carry_the_tavern(serve):
    answers = {
        f"POST /taverns/{TAVERN}/channels/{HALL}/messages": hall_message(),
        f"GET /taverns/{TAVERN}/channels/{HALL}/pins": [hall_message(id="p1")],
    }
    got: list[Message] = []

    async def calls(rest: RESTClient) -> None:
        got.append(await rest.send_message(TAVERN, HALL, content="hello"))
        got.extend(await rest.get_pinned_messages(TAVERN, HALL))

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [(m.id, m.tavern_id) for m in got] == [("m1", TAVERN), ("p1", TAVERN)]


def test_search_sends_the_parameters_each_route_reads(serve):
    found = {"messages": [hall_message(id="s1")], "hasMore": True, "total": 3, "nextOffset": "s1", "engine": "index"}
    answers = {
        f"GET /taverns/{TAVERN}/messages/search": found,
        f"GET /taverns/{TAVERN}/channels/{HALL}/messages/search": found,
    }
    results: list[list[Message]] = []

    async def calls(rest: RESTClient) -> None:
        results.append(await rest.search_messages(TAVERN, query="dragon"))
        results.append(await rest.search_messages(TAVERN, query="dragon", from_user_id=PERSON, offset="s1", limit=10))
        results.append(await rest.search_messages(TAVERN, from_user_id=PERSON, channel_id=HALL))
        results.append(await rest.search_messages(TAVERN, query="dragon", channel_id=HALL, offset="s1"))
        with pytest.raises(ValueError):
            await rest.search_messages(TAVERN, channel_id=HALL)

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [(s["key"].split(" ", 1)[1], s["query"]) for s in recorder.seen] == [
        (f"/taverns/{TAVERN}/messages/search", {"q": "dragon", "limit": "25"}),
        (f"/taverns/{TAVERN}/messages/search",
         {"q": "dragon", "limit": "10", "fromUserId": PERSON, "offset": "s1"}),
        (f"/taverns/{TAVERN}/messages/search", {"limit": "25", "fromUserId": PERSON, "channelId": HALL}),
        (f"/taverns/{TAVERN}/channels/{HALL}/messages/search", {"q": "dragon", "limit": "25", "before": "s1"}),
    ]
    assert all([m.id for m in r] == ["s1"] and r[0].tavern_id == TAVERN for r in results)


# ─── Members ─────────────────────────────────────────────


def test_get_member_reads_the_member_row(serve):
    answers = {
        f"GET /taverns/{TAVERN}/members/{PERSON}": member_row(nickname="Captain", isOwner=True),
    }
    got: list[Member] = []

    async def calls(rest: RESTClient) -> None:
        got.append(await rest.get_member(TAVERN, PERSON))

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    member = got[0]
    assert (member.user_id, member.tavern_id, member.id) == (PERSON, TAVERN, "row-1")
    assert member.display_name == "Captain"
    assert member.user is not None and member.user.display_name == "Ana"
    assert member.roles[0]["name"] == "Crew"
    assert (member.is_online, member.status, member.is_owner, member.is_bot) == (True, "IDLE", True, False)
    assert member.joined_at == "2026-11-20T10:00:00.000Z"


def test_get_members_lists_and_searches(serve):
    bot = member_row(id="row-2", userId="b0b0", user={"id": "b0b0", "displayName": "Helper"}, isBot=True)
    answers = {
        f"GET /taverns/{TAVERN}/members": [member_row(), bot],
    }
    lists: list[list[Member]] = []
    search_answer = {
        "members": [{"id": "row-1", "userId": PERSON, "displayName": "Ana", "avatarUrl": None,
                     "avatarAnimated": False, "nickname": "Cap", "username": "ana"}],
        "count": 1,
    }

    async def calls(rest: RESTClient) -> None:
        lists.append(await rest.get_members(TAVERN))
        recorder.answers[f"GET /taverns/{TAVERN}/members"] = search_answer
        lists.append(await rest.get_members(TAVERN, query="an", limit=5))
        lists.append(await rest.get_members(TAVERN, query="an"))

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [s["query"] for s in recorder.seen] == [{}, {"q": "an", "limit": "5"}, {"q": "an"}]
    everyone, found, _ = lists
    assert [m.display_name for m in everyone] == ["Ana", "Helper"]
    assert everyone[1].is_bot and everyone[1].user is not None and everyone[1].user.is_bot
    assert all(m.tavern_id == TAVERN for m in everyone + found)
    assert (found[0].user_id, found[0].display_name, found[0].user.display_name) == (PERSON, "Cap", "Ana")


def test_kick_sends_the_reason_as_a_json_body(serve):
    async def calls(rest: RESTClient) -> None:
        await rest.kick_member(TAVERN, PERSON, reason="spam")
        await rest.kick_member(TAVERN, PERSON)

    recorder, run = run_with({}, calls)
    asyncio.run(run(serve))
    assert [(s["key"], s["body"]) for s in recorder.seen] == [
        (f"DELETE /taverns/{TAVERN}/members/{PERSON}", {"reason": "spam"}),
        (f"DELETE /taverns/{TAVERN}/members/{PERSON}", None),
    ]


# ─── Roles ───────────────────────────────────────────────


def test_role_helpers_use_the_role_routes(serve):
    role = {"id": ROLE, "name": "Crew", "permissions": "1056", "position": 2}
    answers = {
        f"POST /taverns/{TAVERN}/roles": role,
        f"PATCH /taverns/{TAVERN}/roles/{ROLE}": role,
    }
    answered: list[Any] = []

    async def calls(rest: RESTClient) -> None:
        answered.append(await rest.create_role(
            TAVERN, name="Crew", permissions=Permissions((1 << SEND_MESSAGES) | (1 << KICK_MEMBERS)),
            color="#FF5733", is_hoisted=True,
        ))
        answered.append(await rest.edit_role(TAVERN, ROLE, permissions=0, is_mentionable=False))
        await rest.reorder_roles(TAVERN, {ROLE: 2, "r2": 1})
        await rest.add_member_role(TAVERN, PERSON, ROLE)
        await rest.remove_member_role(TAVERN, PERSON, ROLE)
        await rest.delete_role(TAVERN, ROLE)

    recorder, run = run_with(answers, calls)
    asyncio.run(run(serve))
    assert [(s["key"], s["body"]) for s in recorder.seen] == [
        (f"POST /taverns/{TAVERN}/roles",
         {"name": "Crew", "permissions": "1056", "color": "#FF5733", "isHoisted": True}),
        (f"PATCH /taverns/{TAVERN}/roles/{ROLE}", {"permissions": "0", "isMentionable": False}),
        (f"PATCH /taverns/{TAVERN}/roles/reorder",
         {"roles": [{"id": ROLE, "position": 2}, {"id": "r2", "position": 1}]}),
        (f"POST /taverns/{TAVERN}/members/{PERSON}/roles/{ROLE}", None),
        (f"DELETE /taverns/{TAVERN}/members/{PERSON}/roles/{ROLE}", None),
        (f"DELETE /taverns/{TAVERN}/roles/{ROLE}", None),
    ]
    assert answered == [role, role]


# ─── Interactions ────────────────────────────────────────


def test_an_interaction_response_may_be_embeds_alone(serve):
    embed = Embed(title="Stats")

    async def calls(rest: RESTClient) -> None:
        await rest.reply_to_interaction("i1", embeds=[embed])
        await rest.reply_to_interaction("i1", content="hi", ephemeral=True)
        await rest.follow_up_interaction("i1", content="", embeds=[embed])
        await rest.follow_up_interaction("i1", content="both", embeds=[embed])

    recorder, run = run_with({}, calls)
    asyncio.run(run(serve))
    assert [s["body"] for s in recorder.seen] == [
        {"ephemeral": False, "embeds": [embed.to_dict()]},
        {"ephemeral": True, "content": "hi"},
        {"ephemeral": False, "embeds": [embed.to_dict()]},
        {"ephemeral": False, "content": "both", "embeds": [embed.to_dict()]},
    ]
    assert [s["key"] for s in recorder.seen] == [
        "POST /interactions/i1/callback", "POST /interactions/i1/callback",
        "POST /interactions/i1/followup", "POST /interactions/i1/followup",
    ]
