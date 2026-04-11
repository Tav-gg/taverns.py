"""Embed types and builder for rich bot messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EmbedImage:
    url: str
    width: int | None = None
    height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"url": self.url}
        if self.width is not None:
            d["width"] = self.width
        if self.height is not None:
            d["height"] = self.height
        return d


@dataclass
class EmbedFooter:
    text: str
    icon_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"text": self.text}
        if self.icon_url:
            d["iconUrl"] = self.icon_url
        return d


@dataclass
class EmbedField:
    name: str
    value: str
    inline: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "value": self.value}
        if self.inline:
            d["inline"] = True
        return d


@dataclass
class Embed:
    """A rich embed that can be attached to messages and interaction replies."""
    title: str | None = None
    description: str | None = None
    color: str | None = None          # "#RRGGBB"
    fields: list[EmbedField] = field(default_factory=list)
    footer: EmbedFooter | None = None
    thumbnail: EmbedImage | None = None
    image: EmbedImage | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.color:
            d["color"] = self.color
        if self.fields:
            d["fields"] = [f.to_dict() for f in self.fields]
        if self.footer:
            d["footer"] = self.footer.to_dict()
        if self.thumbnail:
            d["thumbnail"] = self.thumbnail.to_dict()
        if self.image:
            d["image"] = self.image.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Embed:
        fields = [EmbedField(**f) for f in data.get("fields", [])]
        footer_data = data.get("footer")
        if footer_data:
            footer = EmbedFooter(text=footer_data["text"], icon_url=footer_data.get("iconUrl"))
        else:
            footer = None
        thumbnail = EmbedImage(**data["thumbnail"]) if data.get("thumbnail") else None
        image = EmbedImage(**data["image"]) if data.get("image") else None
        return cls(
            title=data.get("title"),
            description=data.get("description"),
            color=data.get("color"),
            fields=fields,
            footer=footer,
            thumbnail=thumbnail,
            image=image,
        )


class EmbedBuilder:
    """Fluent builder for creating embeds.

    Example::

        embed = (
            EmbedBuilder()
            .set_title("Daily Card")
            .set_description("You received a **Rare** card!")
            .set_color("#FFD700")
            .add_field("ATK", "5", inline=True)
            .add_field("DEF", "3", inline=True)
            .set_footer("Taverns TCG")
            .build()
        )
        await interaction.reply("Here's your card:", embeds=[embed])
    """

    def __init__(self) -> None:
        self._data = Embed()

    def set_title(self, title: str) -> EmbedBuilder:
        self._data.title = title
        return self

    def set_description(self, description: str) -> EmbedBuilder:
        self._data.description = description
        return self

    def set_color(self, hex_color: str) -> EmbedBuilder:
        self._data.color = hex_color
        return self

    def add_field(self, name: str, value: str, *, inline: bool = False) -> EmbedBuilder:
        self._data.fields.append(EmbedField(name=name, value=value, inline=inline))
        return self

    def set_footer(self, text: str, *, icon_url: str | None = None) -> EmbedBuilder:
        self._data.footer = EmbedFooter(text=text, icon_url=icon_url)
        return self

    def set_thumbnail(self, url: str) -> EmbedBuilder:
        self._data.thumbnail = EmbedImage(url=url)
        return self

    def set_image(self, url: str) -> EmbedBuilder:
        self._data.image = EmbedImage(url=url)
        return self

    def build(self) -> Embed:
        return self._data
