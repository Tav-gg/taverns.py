"""
taverns.py — Python SDK for building Tavern bots.

Quick start::

    from taverns import Client, Intents

    client = Client(intents=Intents.MESSAGES | Intents.INTERACTIONS)

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user.display_name}")

    @client.event
    async def on_interaction_create(interaction):
        await interaction.reply("Pong!")

    client.run("tavbot_xxx")
"""

from .client import Client
from .commands import command, option
from .embed import Embed, EmbedBuilder, EmbedField, EmbedFooter, EmbedImage
from .errors import (
    AuthenticationError,
    GatewayError,
    HTTPError,
    InteractionError,
    RateLimitError,
    TavernError,
)
from .intents import Intents
from .interaction import Interaction
from .permissions import Permissions
from .rest import RESTClient
from .types import (
    BotCommand,
    BotSelf,
    Channel,
    CommandOption,
    CommandOptionType,
    Member,
    Message,
    Tavern,
    User,
)
from .webhook import verify_webhook_signature

__version__ = "0.2.0"

__all__ = [
    # Core
    "Client",
    "RESTClient",
    "Intents",
    "Permissions",
    # Types
    "User",
    "Tavern",
    "Channel",
    "Member",
    "Message",
    "BotCommand",
    "BotSelf",
    "CommandOption",
    "CommandOptionType",
    # Embeds
    "Embed",
    "EmbedBuilder",
    "EmbedField",
    "EmbedFooter",
    "EmbedImage",
    # Interactions
    "Interaction",
    # Commands
    "command",
    "option",
    # Webhook
    "verify_webhook_signature",
    # Errors
    "TavernError",
    "AuthenticationError",
    "HTTPError",
    "RateLimitError",
    "GatewayError",
    "InteractionError",
]
