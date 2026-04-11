"""Slash command bot — registers /ping and /greet commands."""

import os

from taverns import Client, Intents, Interaction
from taverns.commands import command, option
from taverns.types import CommandOptionType

client = Client(
    intents=Intents.MESSAGES | Intents.INTERACTIONS,
)


@client.event
async def on_ready():
    print(f"Logged in as {client.user.display_name}")

    # Register commands on startup
    await client.register_commands([
        command("ping", "Check bot latency"),
        command("greet", "Greet a user", options=[
            option("name", "Who to greet", CommandOptionType.STRING, required=True),
            option("loud", "Shout the greeting", CommandOptionType.BOOLEAN),
        ]),
    ])
    print("Commands registered")


@client.event
async def on_interaction_create(interaction: Interaction):
    if interaction.command_name == "ping":
        await interaction.reply("Pong!")

    elif interaction.command_name == "greet":
        name = interaction.get_option("name", "World")
        loud = interaction.get_option("loud", False)

        greeting = f"Hello, {name}!"
        if loud:
            greeting = greeting.upper()

        await interaction.reply(greeting)


if __name__ == "__main__":
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("Set BOT_TOKEN environment variable")
        raise SystemExit(1)
    client.run(token)
