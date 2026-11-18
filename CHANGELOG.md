# Changelog

All notable changes to taverns.py are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (while the version is 0.x, a minor release can break things).

## [0.3.0] - 2026-11-18

This release also contains the member moderation methods from 0.2.4, which was tagged but never
published to PyPI.

### Added

- **Voice halls.** `await client.voice.join(hall_id)` connects the bot to a voice hall. It can play
  files, http(s) URLs and raw PCM, with pause, resume, stop and volume. The connection renews itself and
  reports why it ended (`disconnected`). Install with `pip install "taverns.py[voice]"`; ffmpeg is needed
  to play files and URLs.
- **Hearing a voice hall.** With the `LISTEN_VOICE` permission ("Listen in voice halls"), requested by
  the bot and granted by the tavern, a connection emits per-speaker `audio` frames and `speaking` events.
- **Listen-only voice.** A bot with Connect and Listen but no Speak in a hall can join it to hear it.
  `connection.can_speak` (from `canSpeak` in the join answer and in `bot_voice_updated`) says whether the
  bot may play; when it may not, `play()` and `play_pcm()` raise `TavernError`. The audio track is
  published on the first play, never for a listen-only bot. Speak and Listen changes apply live
  (`can_speak` and `listening` events): losing Speak stops playback, getting it back lets the next play
  publish again.
- `client.rest.join_voice()` and `client.rest.leave_voice()`, and the `LISTEN_VOICE` flag in
  `taverns.permissions`.
- Member moderation: `kick_member`, `ban_member`, `unban_member`, `mute_member` and `unmute_member` on
  the client and on `client.rest`.
- **Direct messages.** `await client.send_dm(user_id, content)` opens the bot's DM with a person the
  first time and reuses the channel after that. Also `create_dm`, `send_dm_message`, `get_dm_channel`,
  `get_dm_messages`, `edit_dm_message` and `delete_dm_message` on the client and on `client.rest`, and
  the `DmChannel`, `DmMessage`, `DmMessageDelete`, `DmMessagePage` and `DmAttachment` types.
- The `Intents.DIRECT_MESSAGES` intent (`1 << 16`, not privileged) and the `dm_message_create`,
  `dm_message_update` and `dm_message_delete` events (`on_dm_message_create` and so on).
- `HTTPError.code`: the error code the API sent (for example `"CANNOT_DM_USER"`), with the
  `DMErrorCode` constants. `RateLimitError.scope` says which limit was hit.
- `allow_insecure` option on `Client`, `RESTClient` and `Gateway`, to use plain `http://` and `ws://`
  URLs against a local test server.
- A test suite (`pip install -e ".[dev]"`, then `pytest`), `CHANGELOG.md` and `SECURITY.md`.

### Changed

- **Breaking:** `verify_webhook_signature` raises `ValueError` when the secret is empty or missing, and
  `TypeError` when the body is not bytes or str. Before, an empty secret produced a signature anyone
  could compute.
- **Breaking:** `verify_webhook_signature` always checks that a request is recent (5 minutes by
  default), using the `timestamp` inside the signed body. Before, it trusted the unsigned
  `X-Tavern-Timestamp` header and skipped the check when the header was missing, so an old request could
  be replayed. When the header is passed, it must match the signed timestamp. Pass `max_age=0` to turn
  the check off.
- **Breaking:** custom `api_url` and `ws_url` must use `https://` and `wss://`. Plain `http://` and
  `ws://` are refused unless `allow_insecure=True` is set, because the bot token would travel unencrypted.
- `Permissions.has(LISTEN_VOICE)` is no longer true for `ADMINISTRATOR` alone. Listen in voice halls is
  always granted on its own, as the platform treats it.
- `Intents.DEFAULT` is now `0x17FF9` and `Intents.ALL` is `0x1FFFF`, so both include `DIRECT_MESSAGES`.
- A request over a direct message limit is not sent again: `RateLimitError` is raised at once, with the
  limit in `scope` and the real wait in `retry_after`. A 429 that is still refused after the retries
  now carries the API's message.
- **Breaking:** when the gateway refuses the bot token (HTTP 401 or 403 on connect, or close codes
  4001, 4003 and 4004), the client stops reconnecting and raises `AuthenticationError`. Before, it
  retried every 30 seconds forever.
- The gateway waits briefly (with backoff) before reconnecting after the server closes the connection
  normally, instead of reconnecting at once.
- **REST retries never send a message, a kick or a ban twice.** A request answered with 429 is sent
  again after the wait the API asks for, whatever the method (up to 3 times). After a server error (5xx)
  or a dropped connection, GET and PUT requests are now sent again (up to 3 times, after 1, 2 and 4
  seconds). POST, PATCH and DELETE requests are sent again only when the connection could not be opened
  at all (the host name did not resolve, or the connection was refused), because the API never received
  them. Before, aiohttp itself could send a DELETE (such as a kick or an unban) a second time after a
  dropped connection.
- Requires `aiohttp` 3.14.4 or later and `websockets` 14.0 or later.
- The package declares its license as an SPDX expression (`Apache-2.0`). Building it from source needs
  setuptools 77.0.3 or later.
- `examples/webhook_server.py` refuses to start without `WEBHOOK_SECRET`, however it is launched.

### Security

- The bot token no longer appears in logs. The websockets library logs the connection URL, which
  carries the token, at DEBUG level; those records now go to the `taverns.gateway.ws` logger, which
  replaces the token with `<redacted>` (so does `taverns.gateway`). The token is also URL-encoded now.
- Every ID placed in a REST path is escaped as a single path segment, and empty, `.` and `..` IDs are
  refused. Before, an ID taken from user input (for example `../x`, or text with `?` or `#`) could send
  the bot's request to a different route.
- `aiohttp` 3.14.4 or later is required. Older versions are affected by many published advisories,
  several of them in the HTTP client and response parsing (for example
  [GHSA-cq5v-8q36-5273](https://github.com/advisories/GHSA-cq5v-8q36-5273),
  [GHSA-h4cc-hm5h-fmm9](https://github.com/advisories/GHSA-h4cc-hm5h-fmm9) and
  [GHSA-2g87-pp6c-29x5](https://github.com/advisories/GHSA-2g87-pp6c-29x5)).
- Webhook verification: empty secrets are refused, freshness uses the signed timestamp, and a signature
  header with non-ASCII characters returns `False` instead of raising `TypeError`.

### Fixed

- `Client.start_typing` and `Gateway.is_connected` crashed with `AttributeError` on websockets 14 and
  later (every fresh install).
- After a response with `X-RateLimit-Remaining: 0`, the next request could wait for decades: the reset
  time (Unix seconds) was compared with a monotonic clock. Waits now use the wall clock and never exceed
  60 seconds.
- A `Retry-After` header that is not a number (such as an HTTP date) no longer crashes the request; it
  waits 1 second.
- `rest.get_commands()` and `rest.delete_command()` called the wrong routes; they now use
  `/bots/@me/commands`, like `register_commands()`.
- An answer cut off part way returned `None` as if the request had worked; it now raises (GET and PUT
  are sent again first). An error answer whose body is JSON but not an object no longer raises
  `AttributeError`.
- The `User-Agent` header reports the real SDK version.

## [0.2.3] - 2026-04-12

Earlier versions are described in the git history.

[0.3.0]: https://github.com/Tav-gg/taverns.py/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Tav-gg/taverns.py/tree/v0.2.3
