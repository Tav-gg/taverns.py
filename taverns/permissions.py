"""Tavern permission bitfield utilities."""

from __future__ import annotations

# Permission bit positions (must match Taverns API TavernPermission enum)
VIEW_CHANNELS = 0
MANAGE_CHANNELS = 1
MANAGE_ROLES = 2
MANAGE_TAVERN = 3
CREATE_INVITE = 4
KICK_MEMBERS = 5
BAN_MEMBERS = 6
MANAGE_NICKNAMES = 7
MANAGE_PERMISSIONS = 8
PIN_MESSAGES = 9
SEND_MESSAGES = 10
MANAGE_MESSAGES = 11
MANAGE_STICKERS = 12
ATTACH_FILES = 13
ADD_REACTIONS = 14
MENTION_EVERYONE = 15
EMBED_LINKS = 16
READ_MESSAGE_HISTORY = 17
USE_EXTERNAL_EMOJI = 18
USE_EXTERNAL_STICKERS = 19
MUTE_MEMBERS = 20
BYPASS_SLOWMODE = 21
CONNECT_VOICE = 22
SPEAK = 23
MANAGE_VOICE = 24
VIDEO = 25
CREATE_EVENTS = 26
MANAGE_EVENTS = 27
CREATE_POLLS = 28
VOTE_IN_POLLS = 29
ADMINISTRATOR = 30
CREATE_PUBLIC_THREADS = 31
CREATE_PRIVATE_THREADS = 32
MANAGE_THREADS = 33
SEND_MESSAGES_IN_THREADS = 34
MANAGE_BOTS = 35

_ALL_PERMISSIONS = {
    VIEW_CHANNELS: "VIEW_CHANNELS",
    MANAGE_CHANNELS: "MANAGE_CHANNELS",
    MANAGE_ROLES: "MANAGE_ROLES",
    MANAGE_TAVERN: "MANAGE_TAVERN",
    CREATE_INVITE: "CREATE_INVITE",
    KICK_MEMBERS: "KICK_MEMBERS",
    BAN_MEMBERS: "BAN_MEMBERS",
    MANAGE_NICKNAMES: "MANAGE_NICKNAMES",
    MANAGE_PERMISSIONS: "MANAGE_PERMISSIONS",
    PIN_MESSAGES: "PIN_MESSAGES",
    SEND_MESSAGES: "SEND_MESSAGES",
    MANAGE_MESSAGES: "MANAGE_MESSAGES",
    MANAGE_STICKERS: "MANAGE_STICKERS",
    ATTACH_FILES: "ATTACH_FILES",
    ADD_REACTIONS: "ADD_REACTIONS",
    MENTION_EVERYONE: "MENTION_EVERYONE",
    EMBED_LINKS: "EMBED_LINKS",
    READ_MESSAGE_HISTORY: "READ_MESSAGE_HISTORY",
    USE_EXTERNAL_EMOJI: "USE_EXTERNAL_EMOJI",
    USE_EXTERNAL_STICKERS: "USE_EXTERNAL_STICKERS",
    MUTE_MEMBERS: "MUTE_MEMBERS",
    BYPASS_SLOWMODE: "BYPASS_SLOWMODE",
    CONNECT_VOICE: "CONNECT_VOICE",
    SPEAK: "SPEAK",
    MANAGE_VOICE: "MANAGE_VOICE",
    VIDEO: "VIDEO",
    CREATE_EVENTS: "CREATE_EVENTS",
    MANAGE_EVENTS: "MANAGE_EVENTS",
    CREATE_POLLS: "CREATE_POLLS",
    VOTE_IN_POLLS: "VOTE_IN_POLLS",
    ADMINISTRATOR: "ADMINISTRATOR",
    CREATE_PUBLIC_THREADS: "CREATE_PUBLIC_THREADS",
    CREATE_PRIVATE_THREADS: "CREATE_PRIVATE_THREADS",
    MANAGE_THREADS: "MANAGE_THREADS",
    SEND_MESSAGES_IN_THREADS: "SEND_MESSAGES_IN_THREADS",
    MANAGE_BOTS: "MANAGE_BOTS",
}


class Permissions:
    """Permission bitfield helper.

    Usage::

        perms = Permissions("3072")
        perms.has(SEND_MESSAGES)  # True
        perms.to_list()  # ["SEND_MESSAGES", "READ_MESSAGE_HISTORY"]
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | int = 0):
        self._value = int(value)

    def has(self, permission: int) -> bool:
        if self._value & (1 << ADMINISTRATOR):
            return True
        return bool(self._value & (1 << permission))

    def to_list(self) -> list[str]:
        result = []
        for bit, name in _ALL_PERMISSIONS.items():
            if self._value & (1 << bit):
                result.append(name)
        return result

    def __int__(self) -> int:
        return self._value

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return f"Permissions({self._value})"
