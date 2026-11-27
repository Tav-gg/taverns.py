"""Gateway events reach the handlers as the API sends them. Nothing leaves the machine."""

from __future__ import annotations

import asyncio
from typing import Any

from taverns import Channel, Client, Intents, Member, Tavern
from taverns.gateway import EVENT_NAME_MAP

TAVERN = "7a7a7a7a-1111-4222-8333-444444444444"
HALL = "c0c0c0c0-1111-4222-8333-444444444444"
PERSON = "11111111-2222-4333-8444-555555555555"
BOT_USER = "0b0b0b0b-1111-4222-8333-444444444444"


def collecting_client(*names: str) -> tuple[Client, dict[str, list[Any]]]:
    client = Client()
    seen: dict[str, list[Any]] = {name: [] for name in names}
    for name in names:
        async def handler(*args: Any, _name: str = name) -> None:
            seen[_name].append(args[0] if args else None)

        client.on(name)(handler)
    return client, seen


def test_wire_names_of_the_events():
    assert EVENT_NAME_MAP["tavern_role_created"] == "role_create"
    assert EVENT_NAME_MAP["tavern_role_updated"] == "role_update"
    assert EVENT_NAME_MAP["tavern_role_deleted"] == "role_delete"
    assert EVENT_NAME_MAP["tavern_typing_indicator"] == "typing"
    assert EVENT_NAME_MAP["tavern_member_joined"] == "member_join"
    assert EVENT_NAME_MAP["tavern_message_deleted"] == "message_delete"


def test_intents_match_the_platform():
    assert (Intents.MESSAGES, Intents.MESSAGE_CONTENT, Intents.MEMBERS) == (1, 2, 4)
    assert Intents.ROLES == 1 << 7 and Intents.INTERACTIONS == 1 << 15 and Intents.DIRECT_MESSAGES == 1 << 16
    assert Intents.DEFAULT == 0x17FF9
    assert Intents.ALL == 0x1FFFF
    assert Intents.DEFAULT & (Intents.MESSAGE_CONTENT | Intents.MEMBERS | Intents.INTERACTIONS) == 0
    assert Intents.DEFAULT | Intents.MESSAGE_CONTENT | Intents.MEMBERS | Intents.INTERACTIONS == Intents.ALL


def test_a_person_joining_is_a_member_with_a_name():
    client, seen = collecting_client("member_join")
    event = {
        "tavernId": TAVERN,
        "user": {"id": PERSON, "displayName": "Ana", "avatarUrl": None, "avatarAnimated": False},
        "member": {
            "id": "row-1", "userId": PERSON, "nickname": None, "joinedAt": "2026-11-27T00:00:00.000Z",
            "user": {"id": PERSON, "displayName": "Ana", "avatarUrl": None},
            "roles": [], "isOnline": True, "status": "ONLINE", "isOwner": False, "isBot": False,
        },
    }
    asyncio.run(client._handle_event("member_join", event))
    member = seen["member_join"][0]
    assert isinstance(member, Member)
    assert (member.user_id, member.tavern_id, member.id, member.display_name) == (PERSON, TAVERN, "row-1", "Ana")
    assert member.is_online and not member.is_bot


def test_a_bot_joining_is_a_member_with_a_name():
    client, seen = collecting_client("member_join")
    event = {
        "tavernId": TAVERN,
        "user": {"id": BOT_USER, "displayName": "Helper", "avatarUrl": None,
                 "roles": [{"id": "r1", "name": "Bot: Helper", "position": 1}]},
        "isBot": True,
        "botApplicationId": "app-1",
    }
    asyncio.run(client._handle_event("member_join", event))
    member = seen["member_join"][0]
    assert (member.user_id, member.tavern_id, member.display_name, member.is_bot) == (BOT_USER, TAVERN, "Helper", True)
    assert member.roles[0]["name"] == "Bot: Helper"


def test_a_member_leaving_names_who_left():
    client, seen = collecting_client("member_leave")
    asyncio.run(client._handle_event("member_leave", {"tavernId": TAVERN, "userId": PERSON, "reason": "kicked"}))
    member = seen["member_leave"][0]
    assert (member.user_id, member.tavern_id) == (PERSON, TAVERN)


def test_hall_events_keep_the_hall_cache_right():
    client, seen = collecting_client("channel_create", "channel_update", "channel_delete")

    async def run() -> None:
        await client._handle_event("channel_create", {
            "tavernId": TAVERN, "id": HALL, "name": "general", "type": "TEXT", "position": 0, "topic": None,
        })
        # Some updates carry only what changed
        await client._handle_event("channel_update", {"tavernId": TAVERN, "id": HALL, "isPrivate": True})
        await client._handle_event("channel_update", {"tavernId": TAVERN, "id": HALL, "name": "lobby", "topic": "hi"})
        assert client._channels[HALL].name == "lobby"
        await client._handle_event("channel_delete", {"tavernId": TAVERN, "channelId": HALL})

    asyncio.run(run())
    assert isinstance(seen["channel_update"][0], Channel)
    assert seen["channel_update"][0].name == "general"
    assert seen["channel_update"][1].topic == "hi"
    assert HALL not in client._channels
    assert seen["channel_delete"] == [{"tavernId": TAVERN, "channelId": HALL}]


def test_install_permission_and_removal_events_keep_the_tavern_list():
    client, seen = collecting_client("bot_installed", "bot_permissions_updated", "bot_removed")

    async def run() -> None:
        await client._handle_event("bot_installed", {
            "tavernId": TAVERN, "tavernName": "The Inn", "grantedPermissions": "3072",
        })
        tavern = client.taverns[TAVERN]
        assert isinstance(tavern, Tavern)
        assert (tavern.name, tavern.granted_permissions) == ("The Inn", "3072")
        await client._handle_event("bot_permissions_updated", {"tavernId": TAVERN, "grantedPermissions": "1024"})
        assert client.taverns[TAVERN].granted_permissions == "1024"
        await client._handle_event("bot_removed", {"tavernId": TAVERN})

    asyncio.run(run())
    assert TAVERN not in client.taverns
    assert seen["bot_installed"] == [{"tavernId": TAVERN, "tavernName": "The Inn", "grantedPermissions": "3072"}]
    assert len(seen["bot_permissions_updated"]) == 1 and len(seen["bot_removed"]) == 1


def test_delete_and_pin_events_carry_the_message_id():
    client, seen = collecting_client("message_delete", "message_pin", "typing")
    deleted = {"tavernId": TAVERN, "messageId": "m1", "channelId": HALL}
    pinned = {"tavernId": TAVERN, "messageId": "m1", "channelId": HALL, "message": {"id": "m1"}}
    typing = {"tavernId": TAVERN, "channelId": HALL, "userId": PERSON, "displayName": "Ana", "isTyping": True}

    async def run() -> None:
        await client._handle_event("message_delete", deleted)
        await client._handle_event("message_pin", pinned)
        await client._handle_event("typing", typing)

    asyncio.run(run())
    assert seen["message_delete"] == [deleted]
    assert seen["message_pin"] == [pinned]
    assert seen["typing"] == [typing]
