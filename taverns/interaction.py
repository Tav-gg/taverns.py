"""Interaction model for slash command responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .embed import Embed
    from .rest import RESTClient
    from .types import Message


@dataclass
class Interaction:
    """Represents a slash command interaction from a user.

    Provides convenience methods for responding::

        @client.event
        async def on_interaction_create(interaction):
            if interaction.command_name == "ping":
                await interaction.reply("Pong!")
    """

    id: str
    command_name: str
    tavern_id: str
    channel_id: str
    user_id: str
    options: dict[str, Any] = field(default_factory=dict)

    # Injected by the Client when dispatching the event
    _rest: RESTClient | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: dict, rest: RESTClient | None = None) -> Interaction:
        return cls(
            id=data["id"],
            command_name=data.get("commandName", ""),
            tavern_id=data.get("tavernId", ""),
            channel_id=data.get("channelId", ""),
            user_id=data.get("userId", ""),
            options=data.get("options", {}),
            _rest=rest,
        )

    async def reply(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        """Send an immediate reply to this interaction.

        Args:
            content: The response text (max 4000 chars).
            ephemeral: If True, only the invoking user sees this response.
            embeds: Optional list of rich embeds to attach.
        """
        if not self._rest:
            raise RuntimeError("Interaction is not bound to a REST client")
        await self._rest.reply_to_interaction(
            self.id, content=content, ephemeral=ephemeral, embeds=embeds,
        )

    async def defer_reply(self, *, ephemeral: bool = False) -> None:
        """Acknowledge the interaction with a "thinking..." indicator.

        You must follow up within 15 minutes using :meth:`follow_up`.
        """
        if not self._rest:
            raise RuntimeError("Interaction is not bound to a REST client")
        await self._rest.defer_interaction(self.id, ephemeral=ephemeral)

    async def follow_up(
        self,
        content: str,
        *,
        ephemeral: bool = False,
        embeds: list[Embed] | None = None,
    ) -> None:
        """Send a follow-up message after deferring.

        Args:
            content: The follow-up text.
            ephemeral: If True, only the invoking user sees this response.
            embeds: Optional list of rich embeds to attach.
        """
        if not self._rest:
            raise RuntimeError("Interaction is not bound to a REST client")
        await self._rest.follow_up_interaction(
            self.id, content=content, ephemeral=ephemeral, embeds=embeds,
        )

    async def send_message(
        self,
        content: str,
        *,
        embeds: list[Embed] | None = None,
    ) -> Message:
        """Send a regular message to the channel where the interaction was invoked."""
        if not self._rest:
            raise RuntimeError("Interaction is not bound to a REST client")
        return await self._rest.send_message(
            self.tavern_id, self.channel_id, content=content, embeds=embeds,
        )

    def get_option(self, name: str, default: Any = None) -> Any:
        """Get the value of a command option by name."""
        return self.options.get(name, default)
