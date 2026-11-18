"""Voice halls: play audio, and (when the tavern allows it) hear the hall.

A bot joins one voice hall per tavern. It needs Connect there, plus Speak to
play or Listen to hear (one of the two is enough: a bot with Connect and Listen
but no Speak joins listen-only).

Playing: with Speak (``connection.can_speak``), the first play publishes a
single audio track (48 kHz PCM, or any file or URL ffmpeg can read). Hearing:
only when the bot requests the "Listen in voice halls" permission
(``LISTEN_VOICE``) and the tavern grants it explicitly (Administrator does not
include it); members then see a "listening" marker on the bot.

Needs the optional ``livekit`` package (``pip install "taverns.py[voice]"``).
Playing files or URLs also needs ffmpeg: on your PATH, in the ``FFMPEG_PATH``
environment variable, or passed as ``ffmpeg_path``.

Example::

    connection = await client.voice.join(voice_hall_id)
    await connection.play("music/intro.mp3")
    await connection.leave()
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterable, Callable, Iterable, Union

from .errors import HTTPError, TavernError

logger = logging.getLogger("taverns.voice")

VOICE_SAMPLE_RATE = 48000
"""Every voice hall carries 48 kHz audio."""

_SAMPLES_PER_FRAME = VOICE_SAMPLE_RATE // 50  # 20 ms
_PUBLISH_CHANNELS = 2
_SOURCE_QUEUE_MS = 400
_RENEW_BEFORE_S = 120
_RENEW_RETRY_S = 15
_SESSION_GRACE_S = 120
_REMOVAL_REASON_WAIT_S = 1.5

_RTC_MISSING = (
    'Voice needs the optional package "livekit". Install it with: pip install "taverns.py[voice]"'
)

PcmInput = Union[bytes, bytearray, memoryview, AsyncIterable[bytes], Iterable[bytes], asyncio.StreamReader]


# Tasks started with _spawn. The event loop keeps only weak references to tasks, so
# a task nobody holds can be garbage collected before it finishes.
_background_tasks: set[asyncio.Future[Any]] = set()


def _spawn(awaitable: Any, failure: str) -> None:
    """Run ``awaitable`` in the background and log ``failure`` if it raises."""
    task = asyncio.ensure_future(awaitable)
    _background_tasks.add(task)

    def done(finished: asyncio.Future[Any]) -> None:
        _background_tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error(failure, exc_info=finished.exception())

    task.add_done_callback(done)


def load_rtc(override: Any = None) -> Any:
    """Load ``livekit.rtc``, or explain how to install it."""
    if override is not None:
        return override
    try:
        from livekit import rtc  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TavernError(_RTC_MISSING) from exc
    return rtc


def resolve_ffmpeg_path(explicit: str | None = None) -> str:
    """The ffmpeg binary: explicit, ``FFMPEG_PATH``, or ``ffmpeg`` on PATH."""
    if explicit:
        return explicit
    if os.environ.get("FFMPEG_PATH"):
        return os.environ["FFMPEG_PATH"]
    return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_args(source: str | None, *, volume: float | None = None, seek: float | None = None) -> list[str]:
    """Arguments that turn ``source`` (or stdin) into 48 kHz stereo s16le on stdout."""
    args = ["-hide_banner", "-loglevel", "error"]
    if seek and seek > 0:
        args += ["-ss", str(seek)]
    if source and source.lower().startswith(("http://", "https://")):
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    args += ["-i", source if source else "pipe:0", "-vn"]
    if volume is not None and volume != 1:
        if volume < 0:
            raise ValueError("volume must be >= 0")
        args += ["-af", f"volume={volume}"]
    args += ["-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(VOICE_SAMPLE_RATE), "-ac", str(_PUBLISH_CHANNELS), "pipe:1"]
    return args


def speaker_from_identity(identity: str) -> tuple[str, bool]:
    """``(user_id, is_bot)`` behind a room identity (``<id>:bot`` and ``<id>:obs`` carry the id)."""
    if identity.endswith(":bot"):
        return identity[:-4], True
    if identity.endswith(":obs"):
        return identity[:-4], False
    return identity, False


@dataclass
class VoiceAudioFrame:
    """A 20 ms frame of what one speaker said (only while listening)."""

    user_id: str
    identity: str
    is_bot: bool
    source: str  # "microphone" or "screen_share_audio"
    pcm: bytes  # 48 kHz signed 16-bit little-endian, mono
    sample_rate: int
    channels: int
    samples_per_channel: int


@dataclass
class VoiceSpeakingEvent:
    """Someone started or stopped speaking (only while listening)."""

    user_id: str
    identity: str
    is_bot: bool
    speaking: bool


_CANNOT_SPEAK = (
    "This bot cannot play audio in this voice hall: it does not have the Speak permission "
    "there (it is connected listen-only). Check connection.can_speak before playing."
)


def _upmix(mono_bytes: bytes) -> bytes:
    mono = array("h")
    mono.frombytes(mono_bytes)
    stereo = array("h", bytes(len(mono_bytes) * 2))
    stereo[0::2] = mono
    stereo[1::2] = mono
    return stereo.tobytes()


async def _chunks(source: PcmInput) -> AsyncIterable[bytes]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        yield bytes(source)
        return
    if isinstance(source, asyncio.StreamReader):
        while True:
            chunk = await source.read(65536)
            if not chunk:
                return
            yield chunk
    if hasattr(source, "__aiter__"):
        async for chunk in source:  # type: ignore[union-attr]
            yield bytes(chunk)
        return
    for chunk in source:  # type: ignore[union-attr]
        yield bytes(chunk)


class _Playback:
    def __init__(self) -> None:
        self.stopped = False
        self._running = asyncio.Event()
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set() and not self.stopped

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        self.stopped = True
        self._running.set()

    async def while_paused(self) -> None:
        await self._running.wait()


class VoiceConnection:
    """The bot's place in one voice hall. Get one from ``client.voice.join()``.

    Register handlers with ``@connection.on("disconnected")`` (sync or async).
    Events: ``ready``, ``disconnected(reason)``, ``playing``, ``idle(result)``,
    ``paused``, ``resumed``, ``renewed(expires_at)``, ``listening(bool)``,
    ``can_speak(bool)``, ``audio(VoiceAudioFrame)``, ``speaking(VoiceSpeakingEvent)``,
    ``error(exc)``.

    ``can_speak`` is False for a listen-only bot (no Speak in the hall): playing
    then raises :class:`TavernError`. It follows the tavern's changes live; losing
    Speak stops what is playing.
    """

    def __init__(
        self,
        manager: VoiceManager,
        rtc: Any,
        tavern_id: str,
        channel_id: str,
        join: dict[str, Any],
    ) -> None:
        self._manager = manager
        self._rtc = rtc
        self.tavern_id = tavern_id
        self.channel_id = channel_id
        self._join = join
        self.identity: str = join["identity"]
        self.room: str = join["room"]
        self.expires_at: datetime = _parse_time(join["expiresAt"])
        self.listening: bool = bool(join.get("listening"))
        # Older servers leave canSpeak out, which means True
        self.can_speak: bool = join.get("canSpeak") is not False
        self.state = "connecting"
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._room: Any = None
        self._source: Any = None
        self._track: Any = None
        self._publication: Any = None
        self._publish_lock = asyncio.Lock()
        self._playback: _Playback | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._removal_task: asyncio.Task[None] | None = None
        self._server_reason: str | None = None
        self._streams: dict[str, tuple[Any, asyncio.Task[None]]] = {}
        self._speaking: set[str] = set()

    # ─── Events ──────────────────────────────────────────

    def on(self, event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator: register a handler (sync or async) for an event."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers.setdefault(event, []).append(func)
            return func

        return decorator

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    _spawn(result, f"Error in voice handler for {event}")
            except Exception:
                logger.exception("Error in voice handler for %s", event)

    @property
    def playing(self) -> bool:
        return self._playback is not None and not self._playback.stopped

    @property
    def paused(self) -> bool:
        return self._playback is not None and self._playback.paused

    # ─── Connecting ──────────────────────────────────────

    async def _connect(self) -> None:
        rtc = self._rtc
        room = rtc.Room()
        self._room = room
        room.on("disconnected", self._on_room_disconnected)
        room.on("track_published", self._maybe_subscribe)
        room.on("track_subscribed", self._start_hearing)
        room.on("track_unsubscribed", lambda _track, publication, _p: self._stop_hearing(publication.sid))
        room.on("active_speakers_changed", self._on_speakers)
        # Subscriptions are chosen here (audio only), never automatic. The
        # bot's audio is not end-to-end encrypted; members see that in the apps.
        await room.connect(self._join["url"], self._join["token"], rtc.RoomOptions(auto_subscribe=False))

        # The audio track is published on the first play (and only with Speak)
        self.state = "connected"
        self._schedule_renewal()
        if self.listening:
            self._subscribe_to_all()
        self._emit("ready")

    # ─── Playing ─────────────────────────────────────────

    async def play_pcm(self, source: PcmInput, *, channels: int = 2) -> str:
        """Play raw PCM: 48 kHz, signed 16-bit little-endian, mono or interleaved stereo.

        Stops whatever was playing. Returns ``"finished"`` or ``"stopped"``.
        """
        if channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")
        self._require_connected()
        self._require_speak()
        self.stop()
        playback = _Playback()
        self._playback = playback
        self._emit("playing")
        result = "stopped"
        try:
            await self._ensure_published()
            if not playback.stopped:
                result = await self._pump(_chunks(source), channels, playback)
        finally:
            if self._playback is playback:
                self._playback = None
            playback.stop()
            self._emit("idle", result)
        return result

    async def play(
        self,
        source: str,
        *,
        volume: float | None = None,
        seek: float | None = None,
        ffmpeg_path: str | None = None,
    ) -> str:
        """Play a file path or an http(s) URL through ffmpeg.

        Never pass text a member typed straight in: ffmpeg can open local files
        and many protocols. Stops whatever was playing.
        """
        self._require_connected()
        self._require_speak()
        binary = resolve_ffmpeg_path(ffmpeg_path or self._manager.ffmpeg_path)
        args = ffmpeg_args(source, volume=volume, seek=seek)
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise TavernError(
                f'ffmpeg was not found ("{binary}"). Install ffmpeg and put it on your PATH, '
                "set FFMPEG_PATH, or pass ffmpeg_path."
            ) from exc
        assert proc.stdout is not None
        try:
            result = await self.play_pcm(proc.stdout, channels=2)
        finally:
            if proc.returncode is None:
                proc.kill()
        stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
        code = await proc.wait()
        if result == "finished" and code not in (0, None):
            raise TavernError(f"ffmpeg exited with code {code}: {stderr.strip()[-2000:]}")
        return result

    def pause(self) -> bool:
        """Pause the current playback. False when nothing is playing."""
        if self._playback is None or self._playback.stopped or self._playback.paused:
            return False
        self._playback.pause()
        self._clear_queue()
        self._emit("paused")
        return True

    def resume(self) -> bool:
        """Resume a paused playback. False when nothing is paused."""
        if self._playback is None or not self._playback.paused:
            return False
        self._playback.resume()
        self._emit("resumed")
        return True

    def stop(self) -> None:
        """Stop the current playback (it returns ``"stopped"``)."""
        playback = self._playback
        if playback is None:
            return
        self._playback = None
        playback.stop()
        self._clear_queue()

    async def leave(self) -> None:
        """Leave the hall: the bot leaves the room and the hall's member list."""
        if self.state == "disconnected":
            return
        await self._close("left")
        try:
            await self._manager._rest().leave_voice(self.tavern_id)
        except Exception as exc:
            logger.debug("leave %s: %s", self.tavern_id, exc)

    # ─── Internals ───────────────────────────────────────

    def _require_connected(self) -> None:
        if self.state != "connected":
            raise TavernError("This voice connection is not connected")

    def _require_speak(self) -> None:
        if not self.can_speak:
            raise TavernError(_CANNOT_SPEAK)

    async def _ensure_published(self) -> None:
        """Publish the audio track once (on the first play), if the bot may speak."""
        async with self._publish_lock:
            if self._track is not None:
                return
            rtc = self._rtc
            source = rtc.AudioSource(VOICE_SAMPLE_RATE, _PUBLISH_CHANNELS, queue_size_ms=_SOURCE_QUEUE_MS)
            track = rtc.LocalAudioTrack.create_audio_track("taverns-bot-audio", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            options.dtx = False  # music has quiet passages; keep sending them
            publication = await self._room.local_participant.publish_track(track, options)
            if not self.can_speak or self.state != "connected":
                # Speak was taken away (or the connection ended) while publishing
                await self._drop_track(source, publication)
                self._require_connected()
                self._require_speak()
            self._source, self._track, self._publication = source, track, publication

    async def _drop_track(self, source: Any, publication: Any) -> None:
        """Unpublish a track and close its source (the server may already have removed it)."""
        sid = getattr(publication, "sid", None)
        if sid and self._room is not None:
            try:
                await self._room.local_participant.unpublish_track(sid)
            except Exception:
                pass
        if source is not None:
            try:
                await source.aclose()
            except Exception:
                pass

    def _apply_speak(self, can_speak: bool) -> None:
        if can_speak == self.can_speak:
            return
        self.can_speak = can_speak
        if not can_speak:
            # The server stops the track; stop playing and let the next play
            # (once Speak is back) publish a new one.
            self.stop()
            source, publication = self._source, self._publication
            self._source = self._track = self._publication = None
            _spawn(self._drop_track(source, publication), "Dropping the voice track failed")
        self._emit("can_speak", can_speak)

    def _clear_queue(self) -> None:
        try:
            if self._source is not None:
                self._source.clear_queue()
        except Exception:
            pass

    async def _pump(self, chunks: AsyncIterable[bytes], channels: int, playback: _Playback) -> str:
        rtc = self._rtc
        source = self._source  # kept: losing Speak drops self._source mid-play
        frame_bytes = _SAMPLES_PER_FRAME * channels * 2
        pending = b""

        async def send(data: bytes) -> None:
            if sys.byteorder == "big":
                samples = array("h")
                samples.frombytes(data)
                samples.byteswap()
                data = samples.tobytes()
            stereo = _upmix(data) if channels == 1 else data
            frame = rtc.AudioFrame(stereo, VOICE_SAMPLE_RATE, _PUBLISH_CHANNELS, _SAMPLES_PER_FRAME)
            await source.capture_frame(frame)

        async for chunk in chunks:
            if playback.stopped:
                break
            pending += chunk
            while len(pending) >= frame_bytes:
                await playback.while_paused()
                if playback.stopped or self.state != "connected":
                    break
                data, pending = pending[:frame_bytes], pending[frame_bytes:]
                await send(data)
            if playback.stopped or self.state != "connected":
                break
        if playback.stopped or self.state != "connected":
            return "stopped"
        if len(pending) >= 2:
            usable = len(pending) - (len(pending) % 2)
            await send(pending[:usable] + bytes(frame_bytes - usable))
        await source.wait_for_playout()
        return "stopped" if playback.stopped else "finished"

    def _schedule_renewal(self) -> None:
        if self._renew_task:
            self._renew_task.cancel()
        wait = max(5.0, (self.expires_at - datetime.now(timezone.utc)).total_seconds() - _RENEW_BEFORE_S)
        self._renew_task = asyncio.ensure_future(self._renew_after(wait))

    async def _renew_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        while self.state == "connected":
            try:
                answer = await self._manager._rest().join_voice(self.tavern_id, self.channel_id)
            except HTTPError as exc:
                if exc.status in (400, 403, 404):
                    await self._close("renewal_refused")
                    return
                answer = None
            except Exception:
                answer = None
            if self.state != "connected":
                return
            if answer is not None:
                self.expires_at = _parse_time(answer["expiresAt"])
                self._apply_listening(bool(answer.get("listening")))
                self._apply_speak(answer.get("canSpeak") is not False)
                self._emit("renewed", self.expires_at)
                self._schedule_renewal()
                return
            if datetime.now(timezone.utc).timestamp() >= self.expires_at.timestamp() + _SESSION_GRACE_S:
                await self._close("expired")
                return
            await asyncio.sleep(_RENEW_RETRY_S)

    def _server_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "bot_voice_disconnected":
            self._server_reason = str(data.get("reason") or "connection_lost")
            _spawn(self._close(self._server_reason), "Closing the voice connection failed")
        elif event == "bot_voice_updated":
            if isinstance(data.get("listening"), bool):
                self._apply_listening(data["listening"])
            if isinstance(data.get("canSpeak"), bool):
                self._apply_speak(data["canSpeak"])

    def _apply_listening(self, listening: bool) -> None:
        if listening == self.listening:
            return
        self.listening = listening
        self._emit("listening", listening)
        if listening:
            self._subscribe_to_all()
        else:
            for sid in list(self._streams):
                self._stop_hearing(sid)
            self._speaking.clear()

    def _on_room_disconnected(self, reason: Any = None) -> None:
        if self.state == "disconnected":
            return
        if self._server_reason:
            _spawn(self._close(self._server_reason), "Closing the voice connection failed")
            return

        async def wait_for_reason() -> None:
            await asyncio.sleep(_REMOVAL_REASON_WAIT_S)
            await self._close(self._server_reason or "connection_lost")

        self._removal_task = asyncio.ensure_future(wait_for_reason())

    async def _close(self, reason: str) -> None:
        if self.state == "disconnected":
            return
        self.state = "disconnected"
        current = asyncio.current_task()
        for task in (self._renew_task, self._removal_task):
            if task is not None and task is not current:
                task.cancel()
        self.stop()
        for sid in list(self._streams):
            self._stop_hearing(sid)
        self._speaking.clear()
        try:
            if self._room is not None:
                await self._room.disconnect()
        except Exception:
            pass
        try:
            if self._source is not None:
                await self._source.aclose()
        except Exception:
            pass
        self._manager._forget(self)
        self._emit("disconnected", reason)

    def _heard_source(self, source: Any) -> str | None:
        sources = self._rtc.TrackSource
        if source == sources.SOURCE_MICROPHONE:
            return "microphone"
        if source == sources.SOURCE_SCREENSHARE_AUDIO:
            return "screen_share_audio"
        return None

    def _maybe_subscribe(self, publication: Any, _participant: Any = None) -> None:
        # Audio only: microphones and screen-share sound, never cameras or screens.
        if not self.listening or self.state == "disconnected":
            return
        if self._heard_source(getattr(publication, "source", None)):
            publication.set_subscribed(True)

    def _subscribe_to_all(self) -> None:
        if self._room is None:
            return
        for participant in self._room.remote_participants.values():
            for publication in participant.track_publications.values():
                self._maybe_subscribe(publication, participant)

    def _start_hearing(self, track: Any, publication: Any, participant: Any) -> None:
        source = self._heard_source(getattr(publication, "source", None))
        if not source or not self.listening:
            try:
                publication.set_subscribed(False)
            except Exception:
                pass
            return
        self._stop_hearing(publication.sid)
        identity = participant.identity
        user_id, is_bot = speaker_from_identity(identity)
        stream = self._rtc.AudioStream(track, sample_rate=VOICE_SAMPLE_RATE, num_channels=1)

        async def read() -> None:
            try:
                async for event in stream:
                    frame = event.frame
                    self._emit(
                        "audio",
                        VoiceAudioFrame(
                            user_id=user_id,
                            identity=identity,
                            is_bot=is_bot,
                            source=source,
                            pcm=bytes(frame.data),
                            sample_rate=frame.sample_rate,
                            channels=frame.num_channels,
                            samples_per_channel=frame.samples_per_channel,
                        ),
                    )
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._emit("error", exc)

        self._streams[publication.sid] = (stream, asyncio.ensure_future(read()))

    def _stop_hearing(self, sid: str | None) -> None:
        entry = self._streams.pop(sid, None) if sid else None
        if entry is None:
            return
        stream, task = entry
        task.cancel()
        close = getattr(stream, "aclose", None)
        if close is not None:
            _spawn(close(), "Closing a voice audio stream failed")

    def _on_speakers(self, speakers: list[Any]) -> None:
        if not self.listening:
            return
        now = {s.identity for s in speakers or [] if getattr(s, "identity", "") and s.identity != self.identity}
        for identity in now - self._speaking:
            user_id, is_bot = speaker_from_identity(identity)
            self._emit("speaking", VoiceSpeakingEvent(user_id, identity, is_bot, True))
        for identity in self._speaking - now:
            user_id, is_bot = speaker_from_identity(identity)
            self._emit("speaking", VoiceSpeakingEvent(user_id, identity, is_bot, False))
        self._speaking = now


class VoiceManager:
    """``client.voice``: join and leave voice halls. One connection per tavern;
    joining another hall of the same tavern moves the bot there."""

    def __init__(self, client: Any, *, ffmpeg_path: str | None = None, rtc: Any = None) -> None:
        self._client = client
        self.ffmpeg_path = ffmpeg_path
        self._rtc_override = rtc
        self._connections: dict[str, VoiceConnection] = {}

    def _rest(self) -> Any:
        return self._client.rest

    async def join(self, channel_id: str, *, tavern_id: str | None = None) -> VoiceConnection:
        """Join a voice hall and connect. ``tavern_id`` is needed only when the
        hall is not in the client's channel cache."""
        if tavern_id is None:
            channel = self._client._channels.get(channel_id)
            tavern_id = getattr(channel, "tavern_id", None) or None
        if not tavern_id:
            raise TavernError(f"Unknown voice hall {channel_id}: pass tavern_id= to voice.join()")
        current = self._connections.get(tavern_id)
        if current and current.channel_id == channel_id and current.state == "connected":
            return current
        rtc = load_rtc(self._rtc_override)
        if current and current.state != "disconnected":
            await current._close("moved")  # the server moves the bot
        answer = await self._rest().join_voice(tavern_id, channel_id)
        connection = VoiceConnection(self, rtc, tavern_id, channel_id, answer)
        self._connections[tavern_id] = connection
        try:
            await connection._connect()
        except Exception:
            await connection._close("connection_lost")
            try:
                await self._rest().leave_voice(tavern_id)
            except Exception:
                pass
            raise
        return connection

    def get(self, tavern_id: str) -> VoiceConnection | None:
        """The bot's connection in a tavern, if any."""
        return self._connections.get(tavern_id)

    async def leave(self, tavern_id: str) -> None:
        """Leave the bot's voice hall in a tavern."""
        connection = self._connections.get(tavern_id)
        if connection:
            await connection.leave()
        else:
            await self._rest().leave_voice(tavern_id)

    async def close(self) -> None:
        """Leave every voice hall."""
        for connection in list(self._connections.values()):
            await connection.leave()

    def _forget(self, connection: VoiceConnection) -> None:
        if self._connections.get(connection.tavern_id) is connection:
            del self._connections[connection.tavern_id]

    def _handle_gateway_event(self, event: str, data: dict[str, Any]) -> None:
        if event not in ("bot_voice_disconnected", "bot_voice_updated"):
            return
        connection = self._connections.get(str(data.get("tavernId", "")))
        if connection and connection.channel_id == data.get("channelId"):
            connection._server_event(event, data)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
