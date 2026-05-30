"""REST client for the Taverns API with rate limit handling."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .embed import Embed
from .errors import AuthenticationError, HTTPError, RateLimitError
from .types import BotCommand, BotSelf, Channel, Member, Message, Tavern

logger = logging.getLogger("taverns.rest")

DEFAULT_API_URL = "https://api.tav.gg/api/v1"
MAX_RETRIES = 3


class RESTClient:
    """Async HTTP client for the Taverns REST API.

    Handles ``Bot`` token authentication, rate limit headers, and retry on 429.
    """

    def __init__(self, token: str, *, api_url: str = DEFAULT_API_URL):
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._global_rate_limit: float = 0  # timestamp when global limit expires

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bot {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": "taverns.py/0.2.4",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Make an API request with rate limit handling and retry."""
        session = await self._ensure_session()
        url = f"{self._api_url}{path}"

        for attempt in range(MAX_RETRIES):
            # Wait for global rate limit
            now = asyncio.get_event_loop().time()
            if self._global_rate_limit > now:
                await asyncio.sleep(self._global_rate_limit - now)

            async with session.request(method, url, json=json, params=params) as resp:
                # Track rate limit headers
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining and int(remaining) == 0:
                    reset = float(resp.headers.get("X-RateLimit-Reset", "0"))
                    self._global_rate_limit = reset

                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    logger.warning("Rate limited, retrying after %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status == 401:
                    raise AuthenticationError("Invalid bot token")

                if resp.status == 204:
                    return None

                try:
                    body = await resp.json()
                except Exception:
                    body = None

                if resp.status >= 400:
                    msg = body.get("message", str(resp.status)) if body else str(resp.status)
                    raise HTTPError(resp.status, msg, response_body=body)

                return body

        raise RateLimitError(1.0, "Rate limited after max retries")

    # ─── Convenience methods ─────────────────────────────

    async def get_self(self) -> BotSelf:
        data = await self.request("GET", "/bots/@me")
        return BotSelf.from_dict(data)

    async def get_taverns(self) -> list[Tavern]:
        data = await self.request("GET", "/bots/@me/taverns")
        return [Tavern.from_dict(t) for t in data]

    async def get_channels(self, tavern_id: str) -> list[Channel]:
        data = await self.request("GET", f"/taverns/{tavern_id}/channels")
        return [Channel.from_dict(c) for c in data]

    async def get_members(self, tavern_id: str) -> list[Member]:
        data = await self.request("GET", f"/taverns/{tavern_id}/members")
        return [Member.from_dict(m) for m in data]

    async def send_message(
        self,
        tavern_id: str,
        channel_id: str,
        *,
        content: str,
        reply_to_id: str | None = None,
        embeds: list[Embed] | None = None,
    ) -> Message:
        body: dict[str, Any] = {"content": content}
        if reply_to_id:
            body["replyToId"] = reply_to_id
        if embeds:
            body["embeds"] = [e.to_dict() for e in embeds]
        data = await self.request(
            "POST", f"/taverns/{tavern_id}/channels/{channel_id}/messages", json=body,
        )
        return Message.from_dict(data)

    async def edit_message(
        self,
        tavern_id: str,
        message_id: str,
        *,
        content: str,
        embeds: list[Embed] | None = None,
    ) -> None:
        body: dict[str, Any] = {"content": content}
        if embeds:
            body["embeds"] = [e.to_dict() for e in embeds]
        await self.request(
            "PATCH", f"/taverns/{tavern_id}/messages/{message_id}", json=body,
        )

    async def delete_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/taverns/{tavern_id}/messages/{message_id}")

    async def get_messages(
        self, tavern_id: str, channel_id: str, *, limit: int = 50,
    ) -> list[Message]:
        data = await self.request(
            "GET", f"/taverns/{tavern_id}/channels/{channel_id}/messages",
            params={"limit": str(limit)},
        )
        return [Message.from_dict(m) for m in data]

    # ─── Slash Commands ──────────────────────────────────

    async def register_commands(self, commands: list[dict[str, Any]]) -> list[BotCommand]:
        data = await self.request("PUT", "/bots/@me/commands", json=commands)
        if not data:
            return []
        return [BotCommand.from_dict(c) for c in data]

    async def get_commands(self) -> list[BotCommand]:
        data = await self.request("GET", "/bot/applications/@me/commands")
        return [BotCommand.from_dict(c) for c in data]

    async def delete_command(self, command_id: str) -> None:
        await self.request("DELETE", f"/bot/applications/@me/commands/{command_id}")

    # ─── Interactions ────────────────────────────────────

    async def reply_to_interaction(
        self,
        interaction_id: str,
        *,
        content: str,
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        body: dict[str, Any] = {"content": content, "ephemeral": ephemeral}
        if embeds:
            body["embeds"] = [e.to_dict() for e in embeds]
        await self.request(
            "POST", f"/interactions/{interaction_id}/callback", json=body,
        )

    async def defer_interaction(
        self, interaction_id: str, *, ephemeral: bool = False,
    ) -> None:
        await self.request(
            "POST", f"/interactions/{interaction_id}/defer",
            json={"ephemeral": ephemeral},
        )

    async def follow_up_interaction(
        self,
        interaction_id: str,
        *,
        content: str,
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        body: dict[str, Any] = {"content": content, "ephemeral": ephemeral}
        if embeds:
            body["embeds"] = [e.to_dict() for e in embeds]
        await self.request(
            "POST", f"/interactions/{interaction_id}/followup", json=body,
        )

    # ─── Pins ────────────────────────────────────────────

    async def pin_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("POST", f"/taverns/{tavern_id}/messages/{message_id}/pin")

    async def unpin_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/taverns/{tavern_id}/messages/{message_id}/pin")

    async def get_pinned_messages(self, tavern_id: str, channel_id: str) -> list[Message]:
        data = await self.request("GET", f"/taverns/{tavern_id}/channels/{channel_id}/pins")
        return [Message.from_dict(m) for m in data]

    # ─── Reactions ───────────────────────────────────────

    async def add_reaction(self, tavern_id: str, message_id: str, emoji: str) -> None:
        await self.request(
            "POST", f"/taverns/{tavern_id}/messages/{message_id}/reactions",
            json={"emoji": emoji},
        )

    async def remove_reaction(self, tavern_id: str, message_id: str, emoji: str) -> None:
        from urllib.parse import quote
        await self.request(
            "DELETE", f"/taverns/{tavern_id}/messages/{message_id}/reactions/{quote(emoji)}",
        )

    # ─── Members (extended) ─────────────────────────────

    async def get_member(self, tavern_id: str, user_id: str) -> Member:
        data = await self.request("GET", f"/taverns/{tavern_id}/members/{user_id}")
        return Member.from_dict(data)

    # ─── Member Moderation ──────────────────────────────
    # Each requires the matching granted permission (KICK_MEMBERS /
    # BAN_MEMBERS / MUTE_MEMBERS). The tavern owner is immune to all three —
    # the API rejects any attempt to kick, ban, or mute the owner.

    async def kick_member(
        self, tavern_id: str, user_id: str, *, reason: str | None = None,
    ) -> None:
        """Kick a member from a tavern (requires KICK_MEMBERS)."""
        await self.request(
            "DELETE", f"/taverns/{tavern_id}/members/{user_id}",
            json={"reason": reason} if reason else None,
        )

    async def ban_member(
        self,
        tavern_id: str,
        user_id: str,
        *,
        reason: str | None = None,
        delete_message_seconds: int | None = None,
        auto: bool = False,
    ) -> None:
        """Ban a member from a tavern (requires BAN_MEMBERS)."""
        body: dict[str, Any] = {"userId": user_id}
        if reason:
            body["reason"] = reason
        if delete_message_seconds is not None:
            body["deleteMessageSeconds"] = delete_message_seconds
        if auto:
            body["auto"] = True
        await self.request("POST", f"/taverns/{tavern_id}/bans", json=body)

    async def unban_member(self, tavern_id: str, user_id: str) -> None:
        """Lift a ban (requires BAN_MEMBERS)."""
        await self.request("DELETE", f"/taverns/{tavern_id}/bans/{user_id}")

    async def mute_member(
        self,
        tavern_id: str,
        user_id: str,
        *,
        reason: str | None = None,
        duration_minutes: int | None = None,
        type: str | None = None,
        scope: str | None = None,
        channel_ids: list[str] | None = None,
    ) -> None:
        """Mute/timeout a member (requires MUTE_MEMBERS). Omit duration for permanent."""
        body: dict[str, Any] = {"userId": user_id}
        if reason:
            body["reason"] = reason
        if duration_minutes is not None:
            body["durationMinutes"] = duration_minutes
        if type:
            body["type"] = type
        if scope:
            body["scope"] = scope
        if channel_ids:
            body["channelIds"] = channel_ids
        await self.request("POST", f"/taverns/{tavern_id}/mutes", json=body)

    async def unmute_member(self, tavern_id: str, user_id: str) -> None:
        """Remove an active mute (requires MUTE_MEMBERS)."""
        await self.request("DELETE", f"/taverns/{tavern_id}/mutes/{user_id}")

    # ─── Search ──────────────────────────────────────────

    async def search_messages(
        self, tavern_id: str, *, query: str, channel_id: str | None = None, limit: int = 25,
    ) -> list[Message]:
        params: dict[str, str] = {"q": query, "limit": str(limit)}
        if channel_id:
            path = f"/taverns/{tavern_id}/channels/{channel_id}/messages/search"
        else:
            path = f"/taverns/{tavern_id}/messages/search"
        data = await self.request("GET", path, params=params)
        msgs = data.get("messages", data) if isinstance(data, dict) else data
        return [Message.from_dict(m) for m in msgs]

    # ─── Roles ───────────────────────────────────────────

    async def get_roles(self, tavern_id: str) -> list[dict]:
        return await self.request("GET", f"/taverns/{tavern_id}/roles")
