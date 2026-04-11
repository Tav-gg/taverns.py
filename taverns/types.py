"""Data types for the Taverns SDK. Simple dataclasses matching the API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class CommandOptionType(IntEnum):
    STRING = 1
    INTEGER = 2
    BOOLEAN = 3
    USER = 4
    CHANNEL = 5
    ROLE = 6
    NUMBER = 7


@dataclass
class User:
    id: str
    display_name: str
    avatar_url: str | None = None
    banner_url: str | None = None
    is_bot: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> User:
        return cls(
            id=data["id"],
            display_name=data.get("displayName", ""),
            avatar_url=data.get("avatarUrl"),
            banner_url=data.get("bannerUrl"),
            is_bot=data.get("isBot", False),
        )


@dataclass
class Tavern:
    id: str
    name: str
    slug: str
    icon_url: str | None = None
    member_count: int = 0
    granted_permissions: str = "0"

    @classmethod
    def from_dict(cls, data: dict) -> Tavern:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data.get("slug", ""),
            icon_url=data.get("iconUrl"),
            member_count=data.get("memberCount", 0),
            granted_permissions=data.get("grantedPermissions", "0"),
        )


@dataclass
class Channel:
    id: str
    name: str
    type: str
    tavern_id: str
    position: int = 0
    topic: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Channel:
        return cls(
            id=data["id"],
            name=data["name"],
            type=data.get("type", "TEXT"),
            tavern_id=data.get("tavernId", ""),
            position=data.get("position", 0),
            topic=data.get("topic"),
        )


@dataclass
class Member:
    user_id: str
    tavern_id: str
    nickname: str | None = None
    is_online: bool = False
    is_bot: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Member:
        return cls(
            user_id=data.get("userId", ""),
            tavern_id=data.get("tavernId", ""),
            nickname=data.get("nickname"),
            is_online=data.get("isOnline", False),
            is_bot=data.get("isBot", False),
        )


@dataclass
class Message:
    id: str
    channel_id: str
    content: str
    sender_id: str
    tavern_id: str = ""
    reply_to_id: str | None = None
    metadata: dict = field(default_factory=dict)
    sender_is_bot: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        sender = data.get("sender", {})
        return cls(
            id=data.get("id", ""),
            channel_id=data.get("channelId", ""),
            content=data.get("content", ""),
            sender_id=sender.get("id", data.get("senderId", "")),
            tavern_id=data.get("tavernId", ""),
            reply_to_id=data.get("replyToId"),
            metadata=data.get("metadata", {}),
            sender_is_bot=sender.get("isBot", False),
        )


@dataclass
class BotCommand:
    id: str
    name: str
    description: str
    options: list[CommandOption] = field(default_factory=list)
    default_member_permissions: str | None = None
    version: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> BotCommand:
        options = [CommandOption.from_dict(o) for o in data.get("options", []) or []]
        return cls(
            id=data.get("id", ""),
            name=data["name"],
            description=data["description"],
            options=options,
            default_member_permissions=data.get("defaultMemberPermissions"),
            version=data.get("version", 1),
        )


@dataclass
class CommandOption:
    name: str
    description: str
    type: int
    required: bool = False
    choices: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> CommandOption:
        return cls(
            name=data["name"],
            description=data["description"],
            type=data["type"],
            required=data.get("required", False),
            choices=data.get("choices", []),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "type": self.type,
        }
        if self.required:
            d["required"] = True
        if self.choices:
            d["choices"] = self.choices
        return d


@dataclass
class BotSelf:
    """The bot's own identity, returned by GET /bots/@me."""
    id: str
    name: str
    description: str | None
    user: User
    taverns: list[Tavern]

    @classmethod
    def from_dict(cls, data: dict) -> BotSelf:
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description"),
            user=User.from_dict(data["user"]),
            taverns=[Tavern.from_dict(t) for t in data.get("taverns", [])],
        )
