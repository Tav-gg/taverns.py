"""Main Client class for building Tavern bots.

Usage::

    from taverns import Client, Intents

    client = Client(intents=Intents.MESSAGES | Intents.INTERACTIONS)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user.display_name}")

    @client.event
    async def on_message_create(message):
        if message.content == "!ping":
            await client.send_message(message.tavern_id, message.channel_id, content="Pong!")

    client.run("tavbot_xxx")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from ._url import check_base_url
from .embed import Embed
from .errors import DMErrorCode, HTTPError
from .gateway import Gateway
from .interaction import Interaction
from .rest import RESTClient
from .types import (
    BotCommand,
    BotSelf,
    Channel,
    DmAttachment,
    DmChannel,
    DmMessage,
    DmMessageDelete,
    DmMessagePage,
    Member,
    Message,
    Tavern,
    User,
)
from .voice import VoiceManager

logger = logging.getLogger("taverns")

EventCallback = Callable[..., Coroutine[Any, Any, None]]

# How many person-to-DM-channel IDs the client remembers for send_dm()
DM_CHANNEL_CACHE_SIZE = 10_000


class Client:
    """The main interface for interacting with the Taverns platform.

    Args:
        intents: Gateway intents bitfield (session-level). Use :class:`Intents` constants.
        api_url: Override the REST API base URL (must be ``https://``).
        ws_url: Override the WebSocket gateway URL (must be ``wss://``).
        ffmpeg_path: ffmpeg binary for ``client.voice`` playback (default: ``FFMPEG_PATH`` or PATH).
        allow_insecure: Accept plain ``http://`` and ``ws://`` URLs. The bot token is then
            sent unencrypted, so use this for local development only.
    """

    def __init__(
        self,
        *,
        intents: int | None = None,
        api_url: str = "https://api.tav.gg/api/v1",
        ws_url: str = "wss://ws.tav.gg",
        ffmpeg_path: str | None = None,
        allow_insecure: bool = False,
    ):
        check_base_url(api_url, "api", allow_insecure=allow_insecure)
        check_base_url(ws_url, "gateway", allow_insecure=allow_insecure)
        self._intents = intents
        self._api_url = api_url
        self._ws_url = ws_url
        self._allow_insecure = allow_insecure
        self._token: str | None = None
        self._rest: RESTClient | None = None
        self._gateway: Gateway | None = None
        self._bot_self: BotSelf | None = None
        self._event_handlers: dict[str, list[EventCallback]] = {}
        self._taverns: dict[str, Tavern] = {}
        self._channels: dict[str, Channel] = {}
        # The bot's direct message channel with each person: user_id -> channel_id
        self._dm_channels: dict[str, str] = {}
        # create_dm() calls in flight, so two sends to one person open one DM
        self._dm_opens: dict[str, asyncio.Future[DmChannel]] = {}
        self.voice = VoiceManager(self, ffmpeg_path=ffmpeg_path)
        """Voice halls: ``await client.voice.join(channel_id)`` (needs ``taverns.py[voice]``)."""

    @property
    def user(self) -> User:
        """The bot's own user identity. Available after login."""
        if not self._bot_self:
            raise RuntimeError("Client is not logged in")
        return self._bot_self.user

    @property
    def taverns(self) -> dict[str, Tavern]:
        """Dict of tavern_id -> Tavern for all Taverns the bot is installed in."""
        return self._taverns

    @property
    def rest(self) -> RESTClient:
        """The REST client for direct API calls."""
        if not self._rest:
            raise RuntimeError("Client is not logged in")
        return self._rest

    # ─── Event registration ──────────────────────────────

    def event(self, func: EventCallback) -> EventCallback:
        """Decorator to register an event handler.

        The function name determines the event: ``on_ready``, ``on_message_create``, etc.

        Example::

            @client.event
            async def on_message_create(message):
                print(message.content)
        """
        name = func.__name__
        if name.startswith("on_"):
            name = name[3:]  # strip "on_" prefix
        self._event_handlers.setdefault(name, []).append(func)
        return func

    def on(self, event_name: str) -> Callable[[EventCallback], EventCallback]:
        """Decorator to register an event handler by name.

        Example::

            @client.on("message_create")
            async def handle_msg(message):
                print(message.content)
        """
        def decorator(func: EventCallback) -> EventCallback:
            self._event_handlers.setdefault(event_name, []).append(func)
            return func
        return decorator

    # ─── Convenience methods ─────────────────────────────

    async def send_message(
        self,
        tavern_id: str,
        channel_id: str,
        *,
        content: str,
        reply_to_id: str | None = None,
        embeds: list[Embed] | None = None,
    ) -> Message:
        """Send a message to a channel."""
        return await self.rest.send_message(
            tavern_id, channel_id, content=content, reply_to_id=reply_to_id, embeds=embeds,
        )

    async def start_typing(self, tavern_id: str, channel_id: str) -> None:
        """Send a typing indicator to a channel."""
        if self._gateway and self._gateway.is_connected:
            await self._gateway.send("tavern_typing_start", {"tavernId": tavern_id, "channelId": channel_id})

    async def get_member(self, tavern_id: str, user_id: str) -> Member:
        """Get a specific member of a tavern."""
        return await self.rest.get_member(tavern_id, user_id)

    # ─── Member moderation ───────────────────────────────
    # Require the matching granted permission (KICK_MEMBERS / BAN_MEMBERS /
    # MUTE_MEMBERS). The tavern owner is immune to all three — the API rejects
    # any attempt to kick, ban, or mute the owner.

    async def kick_member(self, tavern_id: str, user_id: str, *, reason: str | None = None) -> None:
        """Kick a member from a tavern (requires KICK_MEMBERS)."""
        await self.rest.kick_member(tavern_id, user_id, reason=reason)

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
        await self.rest.ban_member(
            tavern_id, user_id,
            reason=reason, delete_message_seconds=delete_message_seconds, auto=auto,
        )

    async def unban_member(self, tavern_id: str, user_id: str) -> None:
        """Lift a ban (requires BAN_MEMBERS)."""
        await self.rest.unban_member(tavern_id, user_id)

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
        await self.rest.mute_member(
            tavern_id, user_id,
            reason=reason, duration_minutes=duration_minutes,
            type=type, scope=scope, channel_ids=channel_ids,
        )

    async def unmute_member(self, tavern_id: str, user_id: str) -> None:
        """Remove an active mute (requires MUTE_MEMBERS)."""
        await self.rest.unmute_member(tavern_id, user_id)

    async def register_commands(self, commands: list[dict[str, Any]]) -> list[BotCommand]:
        """Register slash commands for this bot.

        Args:
            commands: List of command dicts. Use :func:`taverns.commands.command` to build.
        """
        return await self.rest.register_commands(commands)

    # ─── Direct messages ─────────────────────────────────
    # The bot may message a person who is in a tavern where the bot is installed
    # and turned on, and who allows it (or wrote to the bot in the last 24 hours).
    # Anything else raises HTTPError with code "CANNOT_DM_USER". Hitting a DM limit
    # raises RateLimitError (scope, retry_after); the SDK does not resend those.

    async def create_dm(self, user_id: str) -> DmChannel:
        """Open the bot's direct message with a person (or get the existing one).

        The channel ID is remembered, so :meth:`send_dm` to the same person does
        not open it again.
        """
        pending = self._dm_opens.get(user_id)
        if pending is None:
            pending = asyncio.ensure_future(self.rest.create_dm(user_id))
            self._dm_opens[user_id] = pending
            pending.add_done_callback(lambda done: self._forget_dm_open(user_id, done))
        # shield: one caller giving up does not cancel the open for the others
        channel = await asyncio.shield(pending)
        self._remember_dm_channel(user_id, channel.id)
        return channel

    async def send_dm(
        self,
        user_id: str,
        content: str | None = None,
        *,
        embeds: list[Embed] | None = None,
        reply_to_id: str | None = None,
        nonce: str | None = None,
        attachments: list[DmAttachment | dict[str, Any]] | None = None,
    ) -> DmMessage:
        """Send a direct message to a person. Opens the DM the first time, then
        reuses the channel.

        Example::

            await client.send_dm(user_id, "Your order is ready!")
            await client.send_dm(user_id, "Your stats:", embeds=[embed])
        """
        options: dict[str, Any] = {
            "embeds": embeds, "reply_to_id": reply_to_id, "nonce": nonce, "attachments": attachments,
        }
        known = self._dm_channels.get(user_id)
        if known:
            try:
                return await self.rest.send_dm_message(known, content, **options)
            except HTTPError as e:
                # The remembered channel is gone: open the DM again, once
                if e.code != DMErrorCode.DM_CHANNEL_NOT_FOUND:
                    raise
                self._dm_channels.pop(user_id, None)
        channel = await self.create_dm(user_id)
        return await self.rest.send_dm_message(channel.id, content, **options)

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
        """Send a message in a direct message the bot already has open."""
        return await self.rest.send_dm_message(
            channel_id, content,
            embeds=embeds, reply_to_id=reply_to_id, nonce=nonce, attachments=attachments,
        )

    async def get_dm_channel(self, channel_id: str) -> DmChannel:
        """Get one of the bot's direct messages."""
        channel = await self.rest.get_dm_channel(channel_id)
        if channel.recipient.id:
            self._remember_dm_channel(channel.recipient.id, channel.id)
        return channel

    async def get_dm_messages(
        self, channel_id: str, *, before: str | None = None, limit: int | None = None,
    ) -> DmMessagePage:
        """Get messages from a direct message, oldest first."""
        return await self.rest.get_dm_messages(channel_id, before=before, limit=limit)

    async def edit_dm_message(
        self,
        channel_id: str,
        message_id: str,
        content: str | None = None,
        *,
        embeds: list[Embed] | None = None,
    ) -> DmMessage:
        """Edit one of the bot's own messages in a direct message."""
        return await self.rest.edit_dm_message(channel_id, message_id, content, embeds=embeds)

    async def delete_dm_message(self, channel_id: str, message_id: str) -> None:
        """Delete one of the bot's own messages in a direct message."""
        await self.rest.delete_dm_message(channel_id, message_id)

    def _remember_dm_channel(self, user_id: str, channel_id: str) -> None:
        self._dm_channels.pop(user_id, None)  # re-insert as the newest entry
        self._dm_channels[user_id] = channel_id
        if len(self._dm_channels) > DM_CHANNEL_CACHE_SIZE:
            self._dm_channels.pop(next(iter(self._dm_channels)), None)

    def _forget_dm_open(self, user_id: str, done: asyncio.Future[DmChannel]) -> None:
        if self._dm_opens.get(user_id) is done:
            del self._dm_opens[user_id]
        if not done.cancelled():
            done.exception()  # the callers see the error; never "exception was never retrieved"

    # ─── Lifecycle ───────────────────────────────────────

    async def login(self, token: str) -> None:
        """Authenticate with the API and connect to the gateway.

        Args:
            token: Bot token (starts with ``tavbot_``).
        """
        self._token = token
        self._rest = RESTClient(token, api_url=self._api_url, allow_insecure=self._allow_insecure)

        # Fetch bot identity
        self._bot_self = await self._rest.get_self()
        self._taverns = {t.id: t for t in self._bot_self.taverns}

        logger.info(
            "Logged in as %s (connected to %d taverns)",
            self.user.display_name, len(self._taverns),
        )

        # Populate channels
        for tavern in self._bot_self.taverns:
            try:
                channels = await self._rest.get_channels(tavern.id)
                for ch in channels:
                    self._channels[ch.id] = ch
            except Exception as e:
                logger.warning("Failed to fetch channels for %s: %s", tavern.id, e)

        # Dispatch ready event
        await self._dispatch("ready")

        # Connect to gateway
        self._gateway = Gateway(
            token,
            intents=self._intents,
            ws_url=self._ws_url,
            on_event=self._handle_event,
            allow_insecure=self._allow_insecure,
        )
        await self._gateway.connect()

    async def close(self) -> None:
        """Disconnect and clean up."""
        try:
            await self.voice.close()
        except Exception as e:
            logger.debug("Leaving voice halls failed: %s", e)
        if self._gateway:
            await self._gateway.disconnect()
        if self._rest:
            await self._rest.close()
        logger.info("Client closed")

    def run(self, token: str) -> None:
        """Blocking call that logs in and runs the bot until interrupted.

        This is the simplest way to start a bot::

            client.run("tavbot_xxx")
        """
        async def _runner() -> None:
            try:
                await self.login(token)
            except KeyboardInterrupt:
                pass
            finally:
                await self.close()

        try:
            asyncio.run(_runner())
        except KeyboardInterrupt:
            pass

    # ─── Internal event dispatch ─────────────────────────

    async def _handle_event(self, event_name: str, data: dict[str, Any]) -> None:
        """Route gateway events to registered handlers."""
        # The bot's own voice connections
        self.voice._handle_gateway_event(event_name, data)

        # Convert data to typed objects where applicable
        if event_name == "message_create":
            await self._dispatch(event_name, Message.from_dict(data))
        elif event_name == "interaction_create":
            await self._dispatch(event_name, Interaction.from_dict(data, rest=self._rest))
        elif event_name in ("dm_message_create", "dm_message_update"):
            dm = DmMessage.from_dict(data)
            if event_name == "dm_message_create" and dm.sender.id and dm.channel_id and not dm.sender.is_bot:
                self._remember_dm_channel(dm.sender.id, dm.channel_id)
            await self._dispatch(event_name, dm)
        elif event_name == "dm_message_delete":
            await self._dispatch(event_name, DmMessageDelete.from_dict(data))
        elif event_name in ("member_join", "member_leave"):
            await self._dispatch(event_name, Member.from_dict(data))
        elif event_name in ("channel_create", "channel_update"):
            channel = Channel.from_dict(data)
            self._channels[channel.id] = channel
            await self._dispatch(event_name, channel)
        elif event_name == "channel_delete":
            ch_id = data.get("id", "")
            self._channels.pop(ch_id, None)
            await self._dispatch(event_name, data)
        elif event_name == "bot_installed":
            tavern = Tavern.from_dict(data) if "name" in data else None
            if tavern:
                self._taverns[tavern.id] = tavern
            await self._dispatch(event_name, data)
        elif event_name == "bot_removed":
            t_id = data.get("tavernId", "")
            self._taverns.pop(t_id, None)
            await self._dispatch(event_name, data)
        else:
            await self._dispatch(event_name, data)

    async def _dispatch(self, event_name: str, *args: Any) -> None:
        """Call all registered handlers for an event."""
        handlers = self._event_handlers.get(event_name, [])
        for handler in handlers:
            try:
                await handler(*args)
            except Exception:
                logger.exception("Error in event handler for %s", event_name)
