"""
Entry point for the Discord music bot (discord.py + Wavelink / Lavalink v4).
Python 3.10+

Run:
    DISCORD_TOKEN=... LAVALINK_URL=http://localhost:2333 LAVALINK_PASSWORD=youshallnotpass python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
import wavelink

from config import settings

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


# ---------------------------
# Discord Bot Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    await ensure_lavalink_connected()
    try:
        await bot.change_presence(activity=discord.Game(name=f"{settings.COMMAND_PREFIX}help"))
    except Exception:
        pass


async def ensure_lavalink_connected():
    """
    Connect a Lavalink Node if none is available.
    Wavelink v3 uses Pool + Node (no NodePool class).
    """
    # If a node is already connected, return.
    try:
        wavelink.Pool.get_node()
        return
    except Exception:
        pass

    node = wavelink.Node(
        uri=settings.LAVALINK_URL,           # e.g. "http://localhost:2333"
        password=settings.LAVALINK_PASSWORD, # e.g. "youshallnotpass"
    )

    log.info("Connecting Lavalink node: %s", settings.LAVALINK_URL)
    await wavelink.Pool.connect(nodes=[node], client=bot)
    log.info("Lavalink node connected.")


@bot.listen("on_wavelink_node_ready")
async def on_node_ready(payload: wavelink.NodeReadyEventPayload):
    log.info("Lavalink node ready: %s (resumed=%s)", payload.node.identifier, payload.resumed)


async def setup_hook():
    await bot.load_extension("cogs.music")
    # ponytail: global sync on every start; fine for one bot, move to a manual command if you hit rate limits
    synced = await bot.tree.sync()
    log.info("Synced %d slash commands.", len(synced))

bot.setup_hook = setup_hook


async def main():
    # Run the bot
    token = os.getenv("DISCORD_TOKEN", settings.DISCORD_TOKEN)
    if not token:
        log.error("DISCORD_TOKEN is not set.")
        sys.exit(1)
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
