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

from .gateway import Gateway
from .interaction import Interaction
from .rest import RESTClient
from .types import BotCommand, BotSelf, Channel, Member, Message, Tavern, User

logger = logging.getLogger("taverns")

EventCallback = Callable[..., Coroutine[Any, Any, None]]


class Client:
    """The main interface for interacting with the Taverns platform.

    Args:
        intents: Gateway intents bitfield (session-level). Use :class:`Intents` constants.
        api_url: Override the REST API base URL.
        ws_url: Override the WebSocket gateway URL.
    """

    def __init__(
        self,
        *,
        intents: int | None = None,
        api_url: str = "https://api.tav.gg/api/v1",
        ws_url: str = "wss://ws.tav.gg",
    ):
        self._intents = intents
        self._api_url = api_url
        self._ws_url = ws_url
        self._token: str | None = None
        self._rest: RESTClient | None = None
        self._gateway: Gateway | None = None
        self._bot_self: BotSelf | None = None
        self._event_handlers: dict[str, list[EventCallback]] = {}
        self._taverns: dict[str, Tavern] = {}
        self._channels: dict[str, Channel] = {}

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
        self, tavern_id: str, channel_id: str, *, content: str, reply_to_id: str | None = None,
    ) -> Message:
        """Send a message to a channel."""
        return await self.rest.send_message(
            tavern_id, channel_id, content=content, reply_to_id=reply_to_id,
        )

    async def register_commands(self, commands: list[dict[str, Any]]) -> list[BotCommand]:
        """Register slash commands for this bot.

        Args:
            commands: List of command dicts. Use :func:`taverns.commands.command` to build.
        """
        return await self.rest.register_commands(commands)

    # ─── Lifecycle ───────────────────────────────────────

    async def login(self, token: str) -> None:
        """Authenticate with the API and connect to the gateway.

        Args:
            token: Bot token (starts with ``tavbot_``).
        """
        self._token = token
        self._rest = RESTClient(token, api_url=self._api_url)

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
        )
        await self._gateway.connect()

    async def close(self) -> None:
        """Disconnect and clean up."""
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
        # Convert data to typed objects where applicable
        if event_name == "message_create":
            obj = Message.from_dict(data)
            await self._dispatch(event_name, obj)
        elif event_name == "interaction_create":
            obj = Interaction.from_dict(data, rest=self._rest)
            await self._dispatch(event_name, obj)
        elif event_name in ("member_join", "member_leave"):
            obj = Member.from_dict(data)
            await self._dispatch(event_name, obj)
        elif event_name in ("channel_create", "channel_update"):
            obj = Channel.from_dict(data)
            self._channels[obj.id] = obj
            await self._dispatch(event_name, obj)
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
