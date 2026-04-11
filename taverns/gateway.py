"""WebSocket gateway client with heartbeat, intents, and auto-reconnect."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Callable, Coroutine

import websockets
import websockets.exceptions

logger = logging.getLogger("taverns.gateway")

DEFAULT_WS_URL = "wss://ws.tav.gg"
HEARTBEAT_INTERVAL = 30  # seconds
MAX_RECONNECT_DELAY = 30  # seconds

# Maps wire event names to friendly Python event names
EVENT_NAME_MAP: dict[str, str] = {
    "tavern_message": "message_create",
    "tavern_message_edited": "message_update",
    "tavern_message_deleted": "message_delete",
    "tavern_messages_bulk_deleted": "message_bulk_delete",
    "tavern_member_joined": "member_join",
    "tavern_member_left": "member_leave",
    "tavern_channel_created": "channel_create",
    "tavern_channel_updated": "channel_update",
    "tavern_channel_deleted": "channel_delete",
    "tavern_role_updated": "role_update",
    "tavern_role_deleted": "role_delete",
    "tavern_updated": "tavern_update",
    "tavern_typing_indicator": "typing",
    "tavern_reaction_updated": "reaction_update",
    "tavern_message_pinned": "message_pin",
    "tavern_message_unpinned": "message_unpin",
    "tavern_voice_state_update": "voice_state_update",
    "tavern_event_created": "event_create",
    "tavern_event_updated": "event_update",
    "tavern_event_deleted": "event_delete",
    "tavern_thread_created": "thread_create",
    "tavern_thread_message": "thread_message",
    "tavern_forum_post_created": "forum_post_create",
    "interaction_create": "interaction_create",
    "interaction_response": "interaction_response",
    "interaction_deferred": "interaction_deferred",
    "bot_installed": "bot_installed",
    "bot_removed": "bot_removed",
    "bot_permissions_updated": "bot_permissions_updated",
    "presence_update": "presence_update",
}


EventHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class Gateway:
    """WebSocket gateway connection to the Taverns platform.

    Connects with bot token authentication and optional session intents.
    Sends heartbeats every 30 seconds and auto-reconnects on disconnect.
    """

    def __init__(
        self,
        token: str,
        *,
        intents: int | None = None,
        ws_url: str = DEFAULT_WS_URL,
        on_event: EventHandler | None = None,
    ):
        self._token = token
        self._intents = intents
        self._ws_url = ws_url
        self._on_event = on_event
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_attempt = 0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.open

    async def connect(self) -> None:
        """Connect to the gateway and start receiving events."""
        self._running = True
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                delay = min(
                    (2 ** self._reconnect_attempt) + random.uniform(0, 1),
                    MAX_RECONNECT_DELAY,
                )
                self._reconnect_attempt += 1
                logger.warning(
                    "Gateway disconnected (%s), reconnecting in %.1fs (attempt %d)",
                    e, delay, self._reconnect_attempt,
                )
                await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        url = f"{self._ws_url}?token={self._token}"
        if self._intents is not None:
            url += f"&intents={self._intents}"

        logger.info("Connecting to gateway: %s", self._ws_url)

        async with websockets.connect(url) as ws:
            self._ws = ws
            self._reconnect_attempt = 0
            logger.info("Gateway connected")

            # Start heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

            try:
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        event_name = data.get("event", "")
                        event_data = data.get("data", {})

                        friendly = EVENT_NAME_MAP.get(event_name, event_name)
                        if self._on_event:
                            await self._on_event(friendly, event_data)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON message from gateway")
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                self._ws = None

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send heartbeat every 30 seconds to keep the connection alive."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send(json.dumps({"action": "heartbeat", "data": {}}))
                logger.debug("Heartbeat sent")
            except websockets.exceptions.ConnectionClosed:
                break

    async def send(self, action: str, data: dict[str, Any] | None = None) -> None:
        """Send a gateway action (e.g., typing indicator)."""
        if self._ws:
            await self._ws.send(json.dumps({"action": action, "data": data or {}}))

    async def disconnect(self) -> None:
        """Gracefully disconnect from the gateway."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
