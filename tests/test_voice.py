"""Voice tests. A fake media module stands in for ``livekit.rtc``, so no network or native code is used."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from taverns import Permissions, TavernError
from taverns.permissions import ADMINISTRATOR, CONNECT_VOICE, LISTEN_VOICE, SPEAK
from taverns.voice import VoiceManager

TAVERN = "tavern-1"
HALL = "hall-1"
BOT = "0b0b0b0b-1111-4222-8333-444444444444"
MEMBER = "11111111-2222-4333-8444-555555555555"
FRAME_BYTES_STEREO = 960 * 2 * 2


class FakeRtc:
    """The parts of ``livekit.rtc`` the SDK uses."""

    def __init__(self) -> None:
        self.rooms: list[Any] = []
        self.sources: list[Any] = []
        fake = self

        class Room:
            def __init__(self) -> None:
                self.handlers: dict[str, Any] = {}
                self.remote_participants: dict[str, Any] = {}
                self.published: list[Any] = []
                self.unpublished: list[str] = []
                room = self

                class LocalParticipant:
                    async def publish_track(self, track: Any, options: Any) -> Any:
                        room.published.append((track, options))
                        return SimpleNamespace(sid=f"TR_local_{len(room.published)}")

                    async def unpublish_track(self, sid: str) -> None:
                        room.unpublished.append(sid)

                self.local_participant = LocalParticipant()
                fake.rooms.append(self)

            def on(self, event: str, handler: Any) -> None:
                self.handlers[event] = handler

            def emit(self, event: str, *args: Any) -> None:
                self.handlers[event](*args)

            async def connect(self, url: str, token: str, options: Any) -> None:
                self.connect_args = (url, token, options)

            async def disconnect(self) -> None:
                self.disconnected = True

        class AudioSource:
            def __init__(self, sample_rate: int, channels: int, queue_size_ms: int = 0) -> None:
                self.sample_rate, self.channels = sample_rate, channels
                self.frames: list[Any] = []
                self.closed = False
                fake.sources.append(self)

            async def capture_frame(self, frame: Any) -> None:
                self.frames.append(frame)
                await asyncio.sleep(0)

            def clear_queue(self) -> None:
                pass

            async def wait_for_playout(self) -> None:
                pass

            async def aclose(self) -> None:
                self.closed = True

        class TrackPublishOptions:
            source: Any = None
            dtx: Any = None

        self.Room = Room
        self.AudioSource = AudioSource
        self.TrackPublishOptions = TrackPublishOptions
        self.AudioFrame = lambda data, rate, channels, samples: SimpleNamespace(
            data=data, sample_rate=rate, num_channels=channels, samples_per_channel=samples,
        )
        self.RoomOptions = lambda auto_subscribe: SimpleNamespace(auto_subscribe=auto_subscribe)
        self.LocalAudioTrack = SimpleNamespace(create_audio_track=lambda name, source: SimpleNamespace(name=name, source=source))
        self.TrackSource = SimpleNamespace(SOURCE_CAMERA=1, SOURCE_MICROPHONE=2, SOURCE_SCREENSHARE=3, SOURCE_SCREENSHARE_AUDIO=4)


def join_answer(**overrides: Any) -> dict[str, Any]:
    answer = {
        "url": "wss://media.example",
        "token": "media-token",
        "identity": f"{BOT}:bot",
        "room": f"tavern_{HALL}",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "listening": False,
    }
    answer.update(overrides)
    return answer


def setup(answers: list[dict[str, Any]] | dict[str, Any] | None = None):
    rtc = FakeRtc()
    queue = answers if isinstance(answers, list) else [answers or join_answer()]
    calls: list[tuple[str, ...]] = []

    async def join_voice(tavern_id: str, channel_id: str) -> dict[str, Any]:
        calls.append(("join", tavern_id, channel_id))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def leave_voice(tavern_id: str) -> dict[str, Any]:
        calls.append(("leave", tavern_id))
        return {"success": True, "left": True}

    client = SimpleNamespace(
        rest=SimpleNamespace(join_voice=join_voice, leave_voice=leave_voice),
        _channels={HALL: SimpleNamespace(tavern_id=TAVERN)},
    )
    return rtc, calls, VoiceManager(client, rtc=rtc)


async def ticks(n: int = 4) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def test_first_play_publishes_one_microphone_track():
    async def run():
        rtc, _, voice = setup(join_answer(canSpeak=True))
        connection = await voice.join(HALL)
        room = rtc.rooms[0]
        assert connection.can_speak is True
        assert room.published == [], "nothing is published before the first play"

        assert await connection.play_pcm(bytes(FRAME_BYTES_STEREO)) == "finished"
        assert await connection.play_pcm(bytes(FRAME_BYTES_STEREO)) == "finished"
        assert len(room.published) == 1, "one track, reused by later plays"
        assert room.published[0][1].source == rtc.TrackSource.SOURCE_MICROPHONE
        assert len(rtc.sources) == 1 and len(rtc.sources[0].frames) == 2
        await connection.leave()

    asyncio.run(run())


def test_answer_without_can_speak_means_the_bot_may_speak():
    async def run():
        _, _, voice = setup(join_answer())
        connection = await voice.join(HALL)
        assert connection.can_speak is True
        await connection.leave()

    asyncio.run(run())


def test_listen_only_joins_hears_never_publishes_and_play_explains():
    async def run():
        rtc, _, voice = setup(join_answer(listening=True, canSpeak=False))
        connection = await voice.join(HALL)
        room = rtc.rooms[0]
        assert connection.state == "connected"
        assert connection.can_speak is False
        assert connection.listening is True

        subscribed: list[bool] = []
        publication = SimpleNamespace(sid="mic", source=rtc.TrackSource.SOURCE_MICROPHONE, set_subscribed=subscribed.append)
        room.emit("track_published", publication, SimpleNamespace(identity=MEMBER))
        assert subscribed == [True], "it still hears the hall"

        with pytest.raises(TavernError, match="does not have the Speak permission"):
            await connection.play_pcm(bytes(FRAME_BYTES_STEREO))
        # Refused before ffmpeg is even looked for
        with pytest.raises(TavernError, match="does not have the Speak permission"):
            await connection.play("song.mp3", ffmpeg_path="definitely-not-ffmpeg-binary")
        assert room.published == []
        assert rtc.sources == []
        assert connection.playing is False
        await connection.leave()

    asyncio.run(run())


def test_losing_speak_stops_playback_and_getting_it_back_publishes_again():
    async def run():
        rtc, _, voice = setup(join_answer(canSpeak=True))
        connection = await voice.join(HALL)
        room = rtc.rooms[0]
        changes: list[bool] = []
        connection.on("can_speak")(changes.append)

        async def endless():
            while True:
                yield bytes(FRAME_BYTES_STEREO)
                await asyncio.sleep(0)

        playing = asyncio.ensure_future(connection.play_pcm(endless()))
        await ticks(6)
        assert len(room.published) == 1

        voice._handle_gateway_event(
            "bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "listening": False, "canSpeak": False},
        )
        assert await asyncio.wait_for(playing, timeout=5) == "stopped"
        await ticks()
        assert connection.can_speak is False
        assert changes == [False]
        assert room.unpublished == ["TR_local_1"]
        assert rtc.sources[0].closed is True
        assert connection.state == "connected", "the bot stays in the hall"
        with pytest.raises(TavernError, match="Speak permission"):
            await connection.play_pcm(bytes(FRAME_BYTES_STEREO))

        voice._handle_gateway_event(
            "bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "listening": False, "canSpeak": True},
        )
        assert changes == [False, True]
        assert await connection.play_pcm(bytes(FRAME_BYTES_STEREO)) == "finished"
        assert len(room.published) == 2, "a new track is published"
        await connection.leave()

    asyncio.run(run())


def test_bot_voice_updated_changes_listening_and_can_speak_independently():
    async def run():
        _, _, voice = setup(join_answer(listening=False, canSpeak=True))
        connection = await voice.join(HALL)
        events: list[tuple[str, bool]] = []
        connection.on("listening")(lambda on: events.append(("listening", on)))
        connection.on("can_speak")(lambda on: events.append(("can_speak", on)))

        voice._handle_gateway_event("bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "canSpeak": False})
        assert connection.listening is False
        voice._handle_gateway_event("bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "listening": True})
        assert connection.can_speak is False
        voice._handle_gateway_event(
            "bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "listening": True, "canSpeak": False},
        )
        assert events == [("can_speak", False), ("listening", True)]
        await connection.leave()

    asyncio.run(run())


def test_speak_lost_while_publishing_fails_the_play_and_unpublishes():
    async def run():
        rtc, _, voice = setup(join_answer(canSpeak=True))
        connection = await voice.join(HALL)
        room = rtc.rooms[0]
        gate = asyncio.Event()

        async def slow_publish(track: Any, options: Any) -> Any:
            room.published.append((track, options))
            await gate.wait()
            return SimpleNamespace(sid="TR_slow")

        room.local_participant.publish_track = slow_publish
        playing = asyncio.ensure_future(connection.play_pcm(bytes(FRAME_BYTES_STEREO)))
        await ticks()
        voice._handle_gateway_event(
            "bot_voice_updated", {"tavernId": TAVERN, "channelId": HALL, "listening": False, "canSpeak": False},
        )
        gate.set()
        with pytest.raises(TavernError, match="Speak permission"):
            await asyncio.wait_for(playing, timeout=5)
        assert room.unpublished == ["TR_slow"]
        assert rtc.sources[0].frames == [], "no audio was sent"
        await connection.leave()

    asyncio.run(run())


def test_renewal_follows_a_change_of_can_speak():
    async def run():
        _, _, voice = setup([join_answer(canSpeak=True), join_answer(canSpeak=False)])
        connection = await voice.join(HALL)
        changes: list[bool] = []
        connection.on("can_speak")(changes.append)
        connection._renew_task.cancel()
        await connection._renew_after(0)
        assert connection.can_speak is False
        assert changes == [False]
        await connection.leave()

    asyncio.run(run())


def test_administrator_does_not_include_listen_in_voice_halls():
    admin = Permissions(1 << ADMINISTRATOR)
    assert admin.has(ADMINISTRATOR)
    assert admin.has(SPEAK)
    assert admin.has(CONNECT_VOICE)
    assert not admin.has(LISTEN_VOICE)
    assert Permissions((1 << ADMINISTRATOR) | (1 << LISTEN_VOICE)).has(LISTEN_VOICE)
    assert Permissions(1 << LISTEN_VOICE).has(LISTEN_VOICE)


def test_background_tasks_are_kept_until_done_and_failures_are_logged(caplog: pytest.LogCaptureFixture):
    from taverns import voice as voice_module

    async def run():
        gate = asyncio.Event()

        async def waits() -> None:
            await gate.wait()

        async def fails() -> None:
            raise RuntimeError("boom")

        voice_module._spawn(waits(), "waiting task failed")
        voice_module._spawn(fails(), "failing task failed")
        assert len(voice_module._background_tasks) == 2, "the loop alone holds only weak references"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(voice_module._background_tasks) == 1
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert voice_module._background_tasks == set()

    with caplog.at_level("ERROR", logger="taverns.voice"):
        asyncio.run(run())
    assert [r.getMessage() for r in caplog.records] == ["failing task failed"]
    assert caplog.records[0].exc_info is not None
