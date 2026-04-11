"""Slash command builder utilities."""

from __future__ import annotations

from typing import Any

from .types import CommandOptionType


def command(
    name: str,
    description: str,
    *,
    options: list[dict[str, Any]] | None = None,
    default_member_permissions: str | None = None,
) -> dict[str, Any]:
    """Build a slash command definition for registration.

    Args:
        name: Command name (lowercase alphanumeric + hyphens, max 32 chars).
        description: Command description (max 100 chars).
        options: List of command options (use :func:`option` to build).
        default_member_permissions: BigInt string of required Tavern permissions.

    Returns:
        dict ready to pass to :meth:`Client.register_commands`.

    Example::

        from taverns.commands import command, option
        from taverns.types import CommandOptionType

        cmds = [
            command("ping", "Check bot latency"),
            command("ban", "Ban a user", options=[
                option("user", "User to ban", CommandOptionType.USER, required=True),
                option("reason", "Ban reason", CommandOptionType.STRING),
            ]),
        ]
        await client.register_commands(cmds)
    """
    cmd: dict[str, Any] = {"name": name, "description": description}
    if options:
        cmd["options"] = options
    if default_member_permissions:
        cmd["defaultMemberPermissions"] = default_member_permissions
    return cmd


def option(
    name: str,
    description: str,
    type: CommandOptionType | int = CommandOptionType.STRING,
    *,
    required: bool = False,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a command option definition.

    Args:
        name: Option name (lowercase alphanumeric + hyphens).
        description: Option description.
        type: Option type (use :class:`CommandOptionType`).
        required: Whether this option is required.
        choices: Preset choices for the option.
    """
    opt: dict[str, Any] = {
        "name": name,
        "description": description,
        "type": int(type),
    }
    if required:
        opt["required"] = True
    if choices:
        opt["choices"] = choices
    return opt
