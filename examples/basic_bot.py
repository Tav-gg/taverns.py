"""Basic echo bot — responds to !ping with Pong!"""

import os

from taverns import Client, Intents, Message

client = Client(
    intents=Intents.MESSAGES | Intents.MESSAGE_CONTENT,
)


@client.event
async def on_ready():
    print(f"Logged in as {client.user.display_name}")
    print(f"Connected to {len(client.taverns)} tavern(s)")


@client.event
async def on_message_create(message: Message):
    # Don't respond to ourselves
    if message.sender_id == client.user.id:
        return

    if message.content == "!ping":
        await client.send_message(
            message.tavern_id,
            message.channel_id,
            content="Pong!",
        )


if __name__ == "__main__":
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("Set BOT_TOKEN environment variable")
        raise SystemExit(1)
    client.run(token)
