"""Gateway intent bitfield — controls which events a bot receives.

Two-layer model:
  - Application-level: BotApplication.allowedIntents (developer configures max)
  - Session-level: declared at connect time via ?intents= query parameter
  - Effective: session & allowed (computed server-side)
"""

from __future__ import annotations


class Intents:
    """Bitfield of gateway intents.

    Usage::

        intents = Intents.MESSAGES | Intents.MESSAGE_CONTENT | Intents.INTERACTIONS
        client = Client(intents=intents)
    """

    MESSAGES: int       = 1 << 0
    MESSAGE_CONTENT: int = 1 << 1   # Privileged
    MEMBERS: int        = 1 << 2   # Privileged
    MODERATION: int     = 1 << 3
    CHANNELS: int       = 1 << 4
    THREADS: int        = 1 << 5
    FORUMS: int         = 1 << 6
    ROLES: int          = 1 << 7
    SETTINGS: int       = 1 << 8
    EVENTS: int         = 1 << 9
    VOICE: int          = 1 << 10
    BROADCASTS: int     = 1 << 11
    TYPING: int         = 1 << 12
    REACTIONS: int      = 1 << 13
    PINS: int           = 1 << 14
    INTERACTIONS: int   = 1 << 15

    # Shortcuts
    DEFAULT: int = 0x7FF9   # All non-privileged (bits 0, 3-15)
    ALL: int     = 0xFFFF   # All intents including privileged

    @staticmethod
    def from_list(*intent_names: str) -> int:
        """Build an intents value from intent names.

        Example::

            intents = Intents.from_list("MESSAGES", "INTERACTIONS")
        """
        mapping = {
            "MESSAGES": Intents.MESSAGES,
            "MESSAGE_CONTENT": Intents.MESSAGE_CONTENT,
            "MEMBERS": Intents.MEMBERS,
            "MODERATION": Intents.MODERATION,
            "CHANNELS": Intents.CHANNELS,
            "THREADS": Intents.THREADS,
            "FORUMS": Intents.FORUMS,
            "ROLES": Intents.ROLES,
            "SETTINGS": Intents.SETTINGS,
            "EVENTS": Intents.EVENTS,
            "VOICE": Intents.VOICE,
            "BROADCASTS": Intents.BROADCASTS,
            "TYPING": Intents.TYPING,
            "REACTIONS": Intents.REACTIONS,
            "PINS": Intents.PINS,
            "INTERACTIONS": Intents.INTERACTIONS,
        }
        value = 0
        for name in intent_names:
            upper = name.upper()
            if upper not in mapping:
                raise ValueError(f"Unknown intent: {name}")
            value |= mapping[upper]
        return value
