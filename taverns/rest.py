"""REST client for the Taverns API with rate limit handling."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import math
import time
from typing import Any

import aiohttp

from ._url import check_base_url
from ._url import path_segment as _seg
from .embed import Embed
from .errors import AuthenticationError, DMErrorCode, HTTPError, RateLimitError
from .permissions import Permissions
from .types import (
    BotCommand,
    BotSelf,
    Channel,
    DmAttachment,
    DmChannel,
    DmMessage,
    DmMessagePage,
    Member,
    Message,
    MessagePage,
    Tavern,
)

logger = logging.getLogger("taverns.rest")

DEFAULT_API_URL = "https://api.tav.gg/api/v1"
MAX_RETRIES = 3  # retries after the first attempt
MAX_RATE_LIMIT_WAIT = 60.0  # seconds: the longest single wait for a rate limit
MAX_BACKOFF = 10.0  # seconds: the longest wait before retrying after an error

# Methods that are sent again after a server error (5xx) or a lost connection, when
# the first attempt may already have reached the API. Reading is harmless and the
# API's PUT routes set or replace a value. POST, PATCH and DELETE are not repeated
# then: a second POST could post a message or ban someone twice, and a repeated
# DELETE (a kick, an unban) fails with 404 although the first one worked.
_REPEATABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT"})


def _backoff(retry: int) -> float:
    """Seconds to wait before retry number ``retry + 1``: 1, 2, 4, at most 10."""
    return min(2.0**retry, MAX_BACKOFF)


def _parse_json(raw: bytes) -> Any:
    """The answer body as JSON, or None when it is empty or not JSON."""
    if not raw.strip():
        return None
    try:
        return _json.loads(raw)
    except ValueError:
        return None


def _never_sent(exc: BaseException) -> bool:
    """True when the request certainly never reached the server: the host name did
    not resolve or the connection could not be opened. TLS failures are left out,
    as a retry would fail the same way."""
    return isinstance(exc, aiohttp.ClientConnectorError) and not isinstance(
        exc, aiohttp.ClientSSLError,
    )


def _rate_limit_wait(reset_at: float, now: float) -> float:
    """Seconds to wait until ``reset_at`` (Unix time in seconds), at most 60."""
    if not math.isfinite(reset_at):
        return 0.0
    return max(0.0, min(reset_at - now, MAX_RATE_LIMIT_WAIT))


def _parse_retry_after(value: str | None) -> float:
    """A ``Retry-After`` header in seconds. Anything else (such as a date) waits 1 second."""
    if value is None:
        return 1.0
    try:
        seconds = float(value)
    except ValueError:
        return 1.0
    if not math.isfinite(seconds) or seconds < 0:
        return 1.0
    return min(seconds, MAX_RATE_LIMIT_WAIT)


def _interaction_body(content: str, ephemeral: bool, embeds: list[Embed] | None) -> dict[str, Any]:
    """An interaction response: the text, the embeds, or both. Empty text is left
    out when there are embeds."""
    body: dict[str, Any] = {"ephemeral": ephemeral}
    if content or not embeds:
        body["content"] = content
    if embeds:
        body["embeds"] = [e.to_dict() for e in embeds]
    return body


def _rate_limit_error(raw: bytes, retry_after_header: str | None) -> RateLimitError:
    """The error for a 429 answer. ``retry_after`` is the real wait (the body's
    ``retryAfter``, else the header), not capped like the wait before a retry."""
    body = _parse_json(raw)
    data: dict[str, Any] | None = body if isinstance(body, dict) else None
    retry_after: float | None = None
    if data is not None:
        value = data.get("retryAfter")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            retry_after = float(value)
    if retry_after is None:
        try:
            seconds = float(retry_after_header) if retry_after_header is not None else 1.0
        except ValueError:
            seconds = 1.0
        retry_after = seconds if math.isfinite(seconds) and seconds >= 0 else 1.0
    scope = data.get("scope") if data is not None else None
    message = data.get("message") if data is not None else None
    return RateLimitError(
        retry_after,
        message if isinstance(message, str) and message else "Rate limited",
        scope=scope if isinstance(scope, str) else None,
        response_body=data,
    )


class RESTClient:
    """Async HTTP client for the Taverns REST API.

    Handles ``Bot`` token authentication, rate limit headers and retries.
    IDs passed to the helper methods are escaped as single path segments, so
    user input cannot change the route; :meth:`request` sends the path you give
    it as is.

    Retries: a request answered with 429 is sent again after the wait the API
    asks for. After a server error (5xx) or a dropped connection, only GET and
    PUT requests are sent again. POST, PATCH and DELETE requests are sent again
    only when the connection could not be opened at all, so a message, a kick
    or a ban is never sent twice. A request over a direct message limit is not
    sent again: :class:`RateLimitError` is raised at once (see its ``scope``).

    Args:
        token: Bot token (starts with ``tavbot_``).
        api_url: Override the REST API base URL (must be ``https://``).
        allow_insecure: Accept a plain ``http://`` API URL. The token is then sent
            unencrypted, so use this for local development only.
    """

    def __init__(
        self, token: str, *, api_url: str = DEFAULT_API_URL, allow_insecure: bool = False,
    ):
        check_base_url(api_url, "api", allow_insecure=allow_insecure)
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        # Unix time (seconds, from X-RateLimit-Reset) until which no requests are left
        self._global_rate_limit: float = 0.0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bot {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": "taverns.py/0.4.0",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
            # By default aiohttp resends a GET, PUT or DELETE once by itself when the
            # connection drops. Retries are decided in request() instead, where a
            # DELETE is never sent twice.
            if "_retry_connection" in getattr(aiohttp.ClientSession, "ATTRS", ()):
                session._retry_connection = False
            self._session = session
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
        """Make an API request with rate limit handling and retry.

        See the class docstring for which failures are retried.
        """
        session = await self._ensure_session()
        url = f"{self._api_url}{path}"
        repeatable = method.upper() in _REPEATABLE_METHODS

        retries = 0
        while True:
            # X-RateLimit-Reset is Unix time in seconds: compare it with the wall clock
            wait = _rate_limit_wait(self._global_rate_limit, time.time())
            if wait > 0:
                logger.debug("No requests left in this rate limit window, waiting %.1fs", wait)
                await asyncio.sleep(wait)
            self._global_rate_limit = 0.0

            try:
                async with session.request(method, url, json=json, params=params) as resp:
                    self._track_rate_limit(resp.headers)
                    status = resp.status
                    retry_after_header = resp.headers.get("Retry-After")
                    retry_after = _parse_retry_after(retry_after_header)
                    # Read the whole answer here: a connection lost while reading it is
                    # handled below like any other lost connection.
                    raw = await resp.read()
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as exc:
                # A lost connection may come after the API received the request, so only
                # methods that are safe to repeat are sent again. Any request is sent again
                # when the connection was never opened.
                if retries < MAX_RETRIES and (repeatable or _never_sent(exc)):
                    delay = _backoff(retries)
                    retries += 1
                    logger.debug(
                        "%s request failed (%s), retrying in %.1fs", method, type(exc).__name__, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

            if status == 429:
                limit_error = _rate_limit_error(raw, retry_after_header)
                # A direct message limit is not waited out: the wait can be hours, and
                # every refused request counts against the bot. The caller decides
                # (scope and retry_after say which limit and how long).
                if limit_error.code == DMErrorCode.BOT_DM_RATE_LIMITED:
                    raise limit_error
                # The API refused the request without acting on it: any method is sent again
                if retries >= MAX_RETRIES:
                    raise limit_error
                retries += 1
                logger.warning("Rate limited, retrying after %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                continue

            if status == 401:
                raise AuthenticationError("Invalid bot token")

            if status >= 500 and repeatable and retries < MAX_RETRIES:
                # The API may have acted on the request before failing; only methods that
                # are safe to repeat are sent again.
                delay = _backoff(retries)
                retries += 1
                logger.debug("%s request got HTTP %d, retrying in %.1fs", method, status, delay)
                await asyncio.sleep(delay)
                continue

            if status == 204:
                return None

            body = _parse_json(raw)

            if status >= 400:
                msg = body.get("message", str(status)) if isinstance(body, dict) else str(status)
                raise HTTPError(status, str(msg), response_body=body if isinstance(body, dict) else None)

            return body

    def _track_rate_limit(self, headers: Any) -> None:
        """Remember when the window resets once a response says no requests are left."""
        try:
            remaining = int(headers.get("X-RateLimit-Remaining", ""))
        except ValueError:
            return
        if remaining > 0:
            self._global_rate_limit = 0.0
            return
        try:
            self._global_rate_limit = float(headers.get("X-RateLimit-Reset", ""))
        except ValueError:
            self._global_rate_limit = time.time() + 1.0

    # ─── Convenience methods ─────────────────────────────

    async def get_self(self) -> BotSelf:
        data = await self.request("GET", "/bots/@me")
        return BotSelf.from_dict(data)

    async def get_taverns(self) -> list[Tavern]:
        data = await self.request("GET", "/bots/@me/taverns")
        return [Tavern.from_dict(t) for t in data]

    # ─── Voice ───────────────────────────────────────────

    async def join_voice(self, tavern_id: str, channel_id: str) -> dict[str, Any]:
        """Join (or renew) a voice hall. Most bots use ``client.voice.join()``,
        which connects and renews for you."""
        return await self.request("POST", f"/bots/@me/taverns/{_seg(tavern_id)}/voice/{_seg(channel_id)}/join")

    async def leave_voice(self, tavern_id: str) -> dict[str, Any]:
        """Leave the bot's voice hall in a tavern."""
        return await self.request("POST", f"/bots/@me/taverns/{_seg(tavern_id)}/voice/leave")

    async def get_channels(self, tavern_id: str) -> list[Channel]:
        """The tavern's halls the bot can see."""
        data = await self.request("GET", f"/taverns/{_seg(tavern_id)}/channels")
        # The answer is {"channels": [...], "categories": [...]}
        rows = data.get("channels") if isinstance(data, dict) else data
        return [Channel.from_dict({"tavernId": tavern_id, **c}) for c in rows or []]

    async def get_members(
        self, tavern_id: str, *, query: str | None = None, limit: int | None = None,
    ) -> list[Member]:
        """The tavern's members.

        With ``query``, only members whose name or nickname contains it, up to
        ``limit`` (1 to 50, default 20). Search results carry the name, avatar
        and nickname, but no roles or online state.
        """
        params: dict[str, str] | None = None
        if query is not None and query.strip():
            params = {"q": query}
            if limit is not None:
                params["limit"] = str(limit)
        data = await self.request("GET", f"/taverns/{_seg(tavern_id)}/members", params=params)
        # A search answers {"members": [...], "count": n}
        rows = data.get("members") if isinstance(data, dict) else data
        return [Member.from_dict(m, tavern_id=tavern_id) for m in rows or []]

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
            "POST", f"/taverns/{_seg(tavern_id)}/channels/{_seg(channel_id)}/messages", json=body,
        )
        return Message.from_dict(data, tavern_id=tavern_id)

    async def edit_message(
        self,
        tavern_id: str,
        message_id: str,
        *,
        content: str,
    ) -> Message:
        """Edit the text of one of the bot's messages (1 to 2000 characters).

        Only the text can be changed; embeds stay as they were sent.
        """
        data = await self.request(
            "PATCH", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}",
            json={"content": content},
        )
        return Message.from_dict(data or {}, tavern_id=tavern_id)

    async def delete_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}")

    async def get_messages(
        self, tavern_id: str, channel_id: str, *, limit: int = 50, before: str | None = None,
    ) -> MessagePage:
        """Get a hall's newest messages, oldest first (``limit`` 1 to 100, default 50).

        For older messages, pass ``before=page.messages[0].id`` while
        ``page.has_more`` is true.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if before:
            params["before"] = before
        data = await self.request(
            "GET", f"/taverns/{_seg(tavern_id)}/channels/{_seg(channel_id)}/messages",
            params=params,
        )
        return MessagePage.from_dict(data or {}, tavern_id=tavern_id)

    # ─── Slash Commands ──────────────────────────────────

    async def register_commands(self, commands: list[dict[str, Any]]) -> list[BotCommand]:
        data = await self.request("PUT", "/bots/@me/commands", json=commands)
        if not data:
            return []
        return [BotCommand.from_dict(c) for c in data]

    async def get_commands(self) -> list[BotCommand]:
        data = await self.request("GET", "/bots/@me/commands")
        return [BotCommand.from_dict(c) for c in data or []]

    async def delete_command(self, command_id: str) -> None:
        await self.request("DELETE", f"/bots/@me/commands/{_seg(command_id)}")

    # ─── Interactions ────────────────────────────────────
    # A response needs text (up to 4000 characters) or at least one embed.

    async def reply_to_interaction(
        self,
        interaction_id: str,
        *,
        content: str = "",
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        await self.request(
            "POST", f"/interactions/{_seg(interaction_id)}/callback",
            json=_interaction_body(content, ephemeral, embeds),
        )

    async def defer_interaction(
        self, interaction_id: str, *, ephemeral: bool = False,
    ) -> None:
        await self.request(
            "POST", f"/interactions/{_seg(interaction_id)}/defer",
            json={"ephemeral": ephemeral},
        )

    async def follow_up_interaction(
        self,
        interaction_id: str,
        *,
        content: str = "",
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        await self.request(
            "POST", f"/interactions/{_seg(interaction_id)}/followup",
            json=_interaction_body(content, ephemeral, embeds),
        )

    # ─── Direct messages ─────────────────────────────────
    # A bot may message a person who shares a tavern with it and allows it (see
    # the Direct Messages guide). Refusals raise HTTPError with code
    # "CANNOT_DM_USER"; DM limits raise RateLimitError at once (not retried).

    async def create_dm(self, user_id: str) -> DmChannel:
        """Open the bot's direct message with a person, or get the existing one."""
        data = await self.request("POST", "/bots/@me/dms", json={"userId": user_id})
        return DmChannel.from_dict(data)

    async def get_dm_channel(self, channel_id: str) -> DmChannel:
        """Get one of the bot's direct messages."""
        data = await self.request("GET", f"/bots/@me/dms/{_seg(channel_id)}")
        return DmChannel.from_dict(data)

    async def get_dm_messages(
        self, channel_id: str, *, before: str | None = None, limit: int | None = None,
    ) -> DmMessagePage:
        """Get messages from a direct message, oldest first (``limit`` 1 to 100, default 50)."""
        params: dict[str, str] = {}
        if before:
            params["before"] = before
        if limit:
            params["limit"] = str(limit)
        data = await self.request(
            "GET", f"/bots/@me/dms/{_seg(channel_id)}/messages", params=params or None,
        )
        return DmMessagePage.from_dict(data or {})

    async def send_dm_message(
        self,
        channel_id: str,
        content: str | None = None,
        *,
        embeds: list[Embed] | None = None,
        reply_to_id: str | None = None,
        nonce: str | None = None,
        attachments: list[DmAttachment | dict[str, Any]] | None = None,
    ) -> DmMessage:
        """Send a message in a direct message the bot already has open.

        It needs ``content`` (up to 2000 characters), an embed (up to 5) or a file
        (up to 10 uploaded files). ``nonce`` is your own ID for this send (up to 64
        characters).
        """
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embeds:
            body["embeds"] = [e.to_dict() for e in embeds]
        if reply_to_id:
            body["replyToId"] = reply_to_id
        if nonce:
            body["nonce"] = nonce
        if attachments:
            body["attachments"] = [
                a.to_dict() if isinstance(a, DmAttachment) else a for a in attachments
            ]
        data = await self.request("POST", f"/bots/@me/dms/{_seg(channel_id)}/messages", json=body)
        return DmMessage.from_dict(data)

    async def edit_dm_message(
        self,
        channel_id: str,
        message_id: str,
        content: str | None = None,
        *,
        embeds: list[Embed] | None = None,
    ) -> DmMessage:
        """Edit one of the bot's own messages in a direct message."""
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embeds is not None:
            body["embeds"] = [e.to_dict() for e in embeds]
        data = await self.request(
            "PATCH", f"/bots/@me/dms/{_seg(channel_id)}/messages/{_seg(message_id)}", json=body,
        )
        return DmMessage.from_dict(data)

    async def delete_dm_message(self, channel_id: str, message_id: str) -> None:
        """Delete one of the bot's own messages in a direct message."""
        await self.request("DELETE", f"/bots/@me/dms/{_seg(channel_id)}/messages/{_seg(message_id)}")

    # ─── Pins ────────────────────────────────────────────

    async def pin_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("POST", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}/pin")

    async def unpin_message(self, tavern_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}/pin")

    async def get_pinned_messages(self, tavern_id: str, channel_id: str) -> list[Message]:
        data = await self.request("GET", f"/taverns/{_seg(tavern_id)}/channels/{_seg(channel_id)}/pins")
        return [Message.from_dict(m, tavern_id=tavern_id) for m in data or []]

    # ─── Reactions ───────────────────────────────────────

    async def add_reaction(self, tavern_id: str, message_id: str, emoji: str) -> None:
        await self.request(
            "POST", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}/reactions",
            json={"emoji": emoji},
        )

    async def remove_reaction(self, tavern_id: str, message_id: str, emoji: str) -> None:
        await self.request(
            "DELETE", f"/taverns/{_seg(tavern_id)}/messages/{_seg(message_id)}/reactions/{_seg(emoji)}",
        )

    # ─── Members (extended) ─────────────────────────────

    async def get_member(self, tavern_id: str, user_id: str) -> Member:
        """One member of a tavern, a person or a bot, by their user ID."""
        data = await self.request("GET", f"/taverns/{_seg(tavern_id)}/members/{_seg(user_id)}")
        return Member.from_dict(data or {}, tavern_id=tavern_id)

    # ─── Member Moderation ──────────────────────────────
    # Each requires the matching granted permission (KICK_MEMBERS /
    # BAN_MEMBERS / MUTE_MEMBERS). The tavern owner is immune to all three —
    # the API rejects any attempt to kick, ban, or mute the owner.

    async def kick_member(
        self, tavern_id: str, user_id: str, *, reason: str | None = None,
    ) -> None:
        """Kick a member from a tavern (requires KICK_MEMBERS).

        ``reason`` (up to 500 characters) is kept in the tavern's audit log.
        """
        await self.request(
            "DELETE", f"/taverns/{_seg(tavern_id)}/members/{_seg(user_id)}",
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
        await self.request("POST", f"/taverns/{_seg(tavern_id)}/bans", json=body)

    async def unban_member(self, tavern_id: str, user_id: str) -> None:
        """Lift a ban (requires BAN_MEMBERS)."""
        await self.request("DELETE", f"/taverns/{_seg(tavern_id)}/bans/{_seg(user_id)}")

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
        await self.request("POST", f"/taverns/{_seg(tavern_id)}/mutes", json=body)

    async def unmute_member(self, tavern_id: str, user_id: str) -> None:
        """Remove an active mute (requires MUTE_MEMBERS)."""
        await self.request("DELETE", f"/taverns/{_seg(tavern_id)}/mutes/{_seg(user_id)}")

    # ─── Search ──────────────────────────────────────────

    async def search_messages(
        self,
        tavern_id: str,
        *,
        query: str | None = None,
        channel_id: str | None = None,
        from_user_id: str | None = None,
        limit: int = 25,
        offset: str | None = None,
    ) -> list[Message]:
        """Search messages, newest first (``limit`` 1 to 50, default 25).

        With ``channel_id`` alone, searches that one hall (or thread) and needs
        ``query``. Otherwise searches every hall the bot can read, with its
        threads, narrowed by ``from_user_id`` (the sender) and ``channel_id``.
        For the next page, pass ``offset=`` the ID of the last result.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if query:
            params["q"] = query
        if channel_id and not from_user_id:
            if not query:
                raise ValueError("Searching one hall needs a query")
            if offset:
                params["before"] = offset
            path = f"/taverns/{_seg(tavern_id)}/channels/{_seg(channel_id)}/messages/search"
        else:
            if from_user_id:
                params["fromUserId"] = from_user_id
            if channel_id:
                params["channelId"] = channel_id
            if offset:
                params["offset"] = offset
            path = f"/taverns/{_seg(tavern_id)}/messages/search"
        data = await self.request("GET", path, params=params)
        msgs = data.get("messages", data) if isinstance(data, dict) else data
        return [Message.from_dict(m, tavern_id=tavern_id) for m in msgs or []]

    # ─── Roles ───────────────────────────────────────────
    # Changing roles needs MANAGE_ROLES. A bot manages only roles below its own
    # highest role, and gives a role only permissions the bot has itself. A
    # newly installed bot's role sits at the bottom; the tavern owner moves it
    # up to let the bot manage roles above it.

    async def get_roles(self, tavern_id: str) -> list[dict]:
        return await self.request("GET", f"/taverns/{_seg(tavern_id)}/roles")

    async def create_role(
        self,
        tavern_id: str,
        *,
        name: str,
        permissions: int | str | Permissions | None = None,
        color: str | None = None,
        is_hoisted: bool | None = None,
        is_mentionable: bool | None = None,
        unicode_emoji: str | None = None,
    ) -> dict:
        """Create a role (``color`` as ``"#RRGGBB"``). It is placed just below the bot's highest role."""
        body: dict[str, Any] = {"name": name}
        body.update(_role_fields(permissions, color, is_hoisted, is_mentionable, unicode_emoji))
        return await self.request("POST", f"/taverns/{_seg(tavern_id)}/roles", json=body)

    async def edit_role(
        self,
        tavern_id: str,
        role_id: str,
        *,
        name: str | None = None,
        permissions: int | str | Permissions | None = None,
        color: str | None = None,
        is_hoisted: bool | None = None,
        is_mentionable: bool | None = None,
        unicode_emoji: str | None = None,
    ) -> dict:
        """Change a role. Fields left as None stay as they are."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        body.update(_role_fields(permissions, color, is_hoisted, is_mentionable, unicode_emoji))
        return await self.request(
            "PATCH", f"/taverns/{_seg(tavern_id)}/roles/{_seg(role_id)}", json=body,
        )

    async def delete_role(self, tavern_id: str, role_id: str) -> None:
        await self.request("DELETE", f"/taverns/{_seg(tavern_id)}/roles/{_seg(role_id)}")

    async def reorder_roles(self, tavern_id: str, positions: dict[str, int]) -> None:
        """Move roles: ``{role_id: position}``. Position 0 is ``@everyone``; higher is above."""
        body = {"roles": [{"id": role_id, "position": position} for role_id, position in positions.items()]}
        await self.request("PATCH", f"/taverns/{_seg(tavern_id)}/roles/reorder", json=body)

    async def add_member_role(self, tavern_id: str, user_id: str, role_id: str) -> None:
        """Give a member a role."""
        await self.request(
            "POST", f"/taverns/{_seg(tavern_id)}/members/{_seg(user_id)}/roles/{_seg(role_id)}",
        )

    async def remove_member_role(self, tavern_id: str, user_id: str, role_id: str) -> None:
        """Take a role from a member."""
        await self.request(
            "DELETE", f"/taverns/{_seg(tavern_id)}/members/{_seg(user_id)}/roles/{_seg(role_id)}",
        )


def _role_fields(
    permissions: int | str | Permissions | None,
    color: str | None,
    is_hoisted: bool | None,
    is_mentionable: bool | None,
    unicode_emoji: str | None,
) -> dict[str, Any]:
    """The optional fields of a role create or edit, as the API names them."""
    fields: dict[str, Any] = {}
    if permissions is not None:
        fields["permissions"] = str(int(permissions))
    if color is not None:
        fields["color"] = color
    if is_hoisted is not None:
        fields["isHoisted"] = is_hoisted
    if is_mentionable is not None:
        fields["isMentionable"] = is_mentionable
    if unicode_emoji is not None:
        fields["unicodeEmoji"] = unicode_emoji
    return fields
