"""WebSocket gateway client with heartbeat, intents, and auto-reconnect."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Callable, Coroutine
from urllib.parse import urlencode

import websockets.exceptions
from websockets.asyncio.client import connect
from websockets.protocol import State

from ._url import check_base_url
from .errors import AuthenticationError

logger = logging.getLogger("taverns.gateway")

DEFAULT_WS_URL = "wss://ws.tav.gg"
HEARTBEAT_INTERVAL = 30  # seconds between a heartbeat and a ping
PONG_TIMEOUT = 75  # seconds without a frame from the gateway before the connection counts as dead
MAX_RECONNECT_DELAY = 30  # seconds
# A connection that stayed up this long starts the reconnect backoff over
STABLE_CONNECTION = 60  # seconds

# Close codes after which the gateway expects the client back at once: the
# connection reached its age limit (1000) or the server is restarting (1012).
RECONNECT_AT_ONCE_CODES = frozenset({1000, 1012})

# ─── Keeping the token out of logs ───────────────────────
# The bot token travels in the connection URL, and the websockets library logs
# the request line at DEBUG level. Its records go to ``taverns.gateway.ws``,
# and both that logger and ``taverns.gateway`` hide ``token=...`` values.

_TOKEN_RE = re.compile(r"(token=)[^&\s#'\"]+", re.IGNORECASE)


def redact_token(text: str) -> str:
    """Replace every ``token=...`` value in ``text`` with ``<redacted>``."""
    return _TOKEN_RE.sub(r"\1<redacted>", text)


class RedactTokenFilter(logging.Filter):
    """Logging filter that hides ``token=...`` values, also in exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if _TOKEN_RE.search(message):
            record.msg = redact_token(message)
            record.args = ()
        if record.exc_info and not record.exc_text:
            text = logging.Formatter().formatException(record.exc_info)
            if _TOKEN_RE.search(text):
                record.exc_text = redact_token(text)
        return True


ws_logger = logging.getLogger("taverns.gateway.ws")
ws_logger.addFilter(RedactTokenFilter())
logger.addFilter(RedactTokenFilter())

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
    "tavern_role_created": "role_create",
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
    "dm_message_create": "dm_message_create",
    "dm_message_update": "dm_message_update",
    "dm_message_delete": "dm_message_delete",
}


EventHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class Gateway:
    """WebSocket gateway connection to the Taverns platform.

    Connects with bot token authentication and optional session intents.
    Every 30 seconds it sends a heartbeat (which keeps the bot online) and a
    ``ping``, which the gateway answers with ``pong``. When nothing arrives for
    75 seconds the connection counts as dead and is opened again.

    Reconnects after every close: at once when the gateway asks for it (close
    code 1000, the connection's age limit, or 1012, a server restart), else
    with a growing delay up to 30 seconds. Stops with
    :class:`AuthenticationError` when the gateway refuses the token, which it
    does when the connection opens (HTTP 401 or 403).

    Args:
        token: Bot token (starts with ``tavbot_``).
        intents: Gateway intents bitfield.
        ws_url: Override the gateway URL (must be ``wss://``).
        on_event: Coroutine called with ``(event_name, data)`` for every event.
        allow_insecure: Accept a plain ``ws://`` URL. The token is then sent
            unencrypted, so use this for local development only.
    """

    def __init__(
        self,
        token: str,
        *,
        intents: int | None = None,
        ws_url: str = DEFAULT_WS_URL,
        on_event: EventHandler | None = None,
        allow_insecure: bool = False,
    ):
        check_base_url(ws_url, "gateway", allow_insecure=allow_insecure)
        self._token = token
        self._intents = intents
        self._ws_url = ws_url
        self._on_event = on_event
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_attempt = 0
        # Monotonic time of the last frame from the gateway (a pong or an event)
        self._last_frame_at = 0.0
        # How the last connection ended: the close code the gateway sent, if any
        self._close_code: int | None = None
        self._opened_at: float | None = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    async def connect(self) -> None:
        """Connect to the gateway and receive events until :meth:`disconnect`.

        Reconnects after a disconnect (see the class docstring for the delay).
        Raises :class:`AuthenticationError`, without reconnecting, when the
        gateway refuses the bot token (HTTP 401 or 403 when connecting).
        """
        self._running = True
        while self._running:
            reason: object = "connection closed"
            self._close_code = None
            self._opened_at = None
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                break
            except AuthenticationError as e:
                self._running = False
                logger.error("%s. Not reconnecting.", e)
                raise
            except Exception as e:
                reason = e
            if not self._running:
                break
            if self._close_code is not None:
                reason = f"close code {self._close_code}"
            delay = self._reconnect_delay()
            logger.warning(
                "Gateway disconnected (%s), reconnecting in %.1fs (attempt %d)",
                reason, delay, self._reconnect_attempt,
            )
            await asyncio.sleep(delay)

    def _reconnect_delay(self) -> float:
        """Seconds to wait before the next connection attempt."""
        lasted = time.monotonic() - self._opened_at if self._opened_at is not None else 0.0
        if lasted >= STABLE_CONNECTION:
            self._reconnect_attempt = 0
        if self._close_code in RECONNECT_AT_ONCE_CODES and self._opened_at is not None:
            # The gateway closed a working connection on purpose and expects it back
            self._reconnect_attempt = 1
            return random.uniform(0, 1)
        delay = min((2 ** self._reconnect_attempt) + random.uniform(0, 1), MAX_RECONNECT_DELAY)
        self._reconnect_attempt += 1
        return delay

    def _connect_url(self) -> str:
        query = {"token": self._token}
        if self._intents is not None:
            query["intents"] = str(self._intents)
        separator = "&" if "?" in self._ws_url else "?"
        return f"{self._ws_url}{separator}{urlencode(query)}"

    async def _connect_once(self) -> None:
        # Only the base URL is logged, never the URL with the token
        logger.info("Connecting to gateway: %s", self._ws_url)

        try:
            async with connect(self._connect_url(), logger=ws_logger) as ws:
                self._ws = ws
                self._opened_at = time.monotonic()
                self._last_frame_at = self._opened_at
                logger.info("Gateway connected")

                # Start heartbeat
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

                try:
                    async for raw in ws:
                        self._last_frame_at = time.monotonic()
                        try:
                            # Frames carry message content and user data: DEBUG level only
                            logger.debug(
                                "Gateway raw message: %s",
                                raw[:200] if isinstance(raw, str) else str(raw)[:200],
                            )
                            data = json.loads(raw)
                            event_name = data.get("event", "")
                            event_data = data.get("data")
                            if event_data is None:
                                event_data = {}
                        except (json.JSONDecodeError, AttributeError):
                            logger.warning("Received a frame that is not a JSON event from the gateway")
                            continue
                        if event_name == "pong":
                            continue  # the answer to our ping; it only proves the gateway is there

                        friendly = EVENT_NAME_MAP.get(event_name, event_name)
                        if self._on_event:
                            try:
                                await self._on_event(friendly, event_data)
                            except Exception:
                                # One event that cannot be handled never drops the connection
                                logger.exception("Error while handling gateway event %s", friendly)
                finally:
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                        try:
                            await self._heartbeat_task
                        except asyncio.CancelledError:
                            pass
                    self._close_code = ws.close_code
                    self._ws = None
        except websockets.exceptions.InvalidStatus as e:
            status = e.response.status_code
            if status in (401, 403):
                raise AuthenticationError(
                    f"Authentication failed: the gateway refused the bot token (HTTP {status})"
                ) from None
            raise

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Every 30 seconds: send a heartbeat (it keeps the bot online) and a ping
        (the gateway answers ``pong``). Closes the connection when nothing has
        come from the gateway for 75 seconds, so it is opened again."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            silent = time.monotonic() - self._last_frame_at
            if silent > PONG_TIMEOUT:
                logger.warning("No answer from the gateway for %.0fs, reconnecting", silent)
                await ws.close(code=1001, reason="no pong")
                break
            try:
                await ws.send(json.dumps({"action": "heartbeat", "data": {}}))
                await ws.send(json.dumps({"action": "ping"}))
                logger.debug("Heartbeat and ping sent")
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
