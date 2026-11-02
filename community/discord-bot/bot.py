"""The Narratarr Discord bot: react with 🎧 on the welcome post to unlock posting.

It does one job. Someone adds 🎧 to the pinned welcome post in #welcome: they get the Listener role.
They take it off: the role goes too. Any other reaction on that post is removed, so it stays tidy.
When it starts (after an update, or a restart), it hands the role to anyone who reacted while it was away.

Configuration, from the environment:
  DISCORD_TOKEN      the bot's token (or DISCORD_TOKEN_FILE: a file holding it)
  GATE_EMOJI         the unlock emoji (default 🎧)
  GATE_ROLE          the role it gives (default Listener)
  WELCOME_CHANNEL    a word in the welcome channel's name (default welcome)

It needs these permissions: View Channels, Read Message History, Add Reactions, Manage Messages
(to remove stray reactions) and Manage Roles, and its own role must sit above the Listener role.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import discord

log = logging.getLogger("narratarr-discord")

GATE = os.environ.get("GATE_EMOJI", "🎧")
ROLE = os.environ.get("GATE_ROLE", "Listener")
WELCOME = os.environ.get("WELCOME_CHANNEL", "welcome")


def token() -> str:
    if os.environ.get("DISCORD_TOKEN"):
        return os.environ["DISCORD_TOKEN"].strip()
    if os.environ.get("DISCORD_TOKEN_FILE"):
        return Path(os.environ["DISCORD_TOKEN_FILE"]).read_text().strip()
    sys.exit("Set DISCORD_TOKEN (or DISCORD_TOKEN_FILE) to the bot's token.")


class Gatekeeper(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_reactions = True  # no privileged intents needed
        super().__init__(intents=intents, activity=discord.Activity(type=discord.ActivityType.watching,
                                                                     name=f"for {GATE} in #welcome"))
        self.post: discord.Message | None = None
        self.role: discord.Role | None = None

    async def find_post(self) -> None:
        """The pinned post in #welcome that this bot wrote, and the role it hands out."""
        self.post = self.role = None
        for guild in self.guilds:
            self.role = discord.utils.get(guild.roles, name=ROLE)
            channel = next((c for c in guild.text_channels if WELCOME in c.name), None)
            if not channel:
                continue
            for message in await channel.pins():
                if message.author.id == self.user.id:
                    self.post = message
                    return

    async def on_ready(self) -> None:
        await self.find_post()
        if not self.post or not self.role:
            log.error("Couldn't find the welcome post or the %s role. Is the bot in the server?", ROLE)
            return
        log.info("Watching %s on the welcome post in #%s for the %s role", GATE, self.post.channel.name, ROLE)
        await self.catch_up()

    async def catch_up(self) -> None:
        """Give the role to anyone who reacted while the bot was off, and clear stray reactions."""
        post = await self.post.channel.fetch_message(self.post.id)
        given = 0
        for reaction in post.reactions:
            if str(reaction.emoji) != GATE:
                await post.clear_reaction(reaction.emoji)
                continue
            async for user in reaction.users():
                if user.bot:
                    continue
                member = post.guild.get_member(user.id) or await self.fetch_member(post.guild, user.id)
                if member and self.role not in member.roles:
                    await member.add_roles(self.role, reason=f"Reacted with {GATE} while the bot was away")
                    given += 1
        if given:
            log.info("Caught up: gave %s to %d people", ROLE, given)

    async def fetch_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:  # they left
            return None

    def is_post(self, payload: discord.RawReactionActionEvent) -> bool:
        return self.post is not None and payload.message_id == self.post.id

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self.is_post(payload) or payload.user_id == self.user.id:
            return
        if str(payload.emoji) != GATE:  # keep the welcome post to just the one reaction
            await self.post.remove_reaction(payload.emoji, discord.Object(payload.user_id))
            return
        member = payload.member or await self.fetch_member(self.post.guild, payload.user_id)
        if member and not member.bot and self.role not in member.roles:
            await member.add_roles(self.role, reason=f"Reacted with {GATE} on the welcome post")
            log.info("Unlocked %s", member)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not self.is_post(payload) or str(payload.emoji) != GATE:
            return
        member = await self.fetch_member(self.post.guild, payload.user_id)
        if member and self.role in member.roles:
            await member.remove_roles(self.role, reason=f"Took {GATE} off the welcome post")
            log.info("Locked %s again", member)

    async def on_guild_channel_pins_update(self, channel, last_pin) -> None:
        if WELCOME in getattr(channel, "name", ""):
            await self.find_post()  # the welcome post was re-pinned or replaced


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    Gatekeeper().run(token(), log_handler=None)


if __name__ == "__main__":
    main()
