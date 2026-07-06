"""The Discord client for TadokuBot.

``TadokuBot`` subclasses ``discord.ext.commands.Bot`` and wires up the three
things every cog relies on:

  * a single shared ``aiohttp.ClientSession`` for all tadoku.app calls,
  * automatic loading of every cog in the ``cogs/`` folder, and
  * a global slash-command error handler that turns exceptions into friendly,
    ephemeral messages instead of raw tracebacks.
"""

import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from lib.permissions import NotAdmin

_log = logging.getLogger(__name__)


class TadokuBot(commands.Bot):
    def __init__(self, cog_folder: str = "cogs", admin_roles: set[str] | None = None):
        # This bot only uses slash commands and only reads public data, so it
        # needs no privileged intents. Sticking to the default intents means
        # the bot works without ticking "Message Content" / "Server Members"
        # in the Developer Portal and avoids Discord's verification gate.
        intents = discord.Intents.default()
        super().__init__(
            # There are no text (prefix) commands; requiring an @-mention as the
            # "prefix" effectively disables them while satisfying the base class.
            command_prefix=commands.when_mentioned,
            intents=intents,
            # Never ping anyone: leaderboard/embeds echo user display names from
            # tadoku.app, and we don't want a display name like "@everyone" to
            # actually notify the server.
            allowed_mentions=discord.AllowedMentions.none(),
            # We ship our own /help-free command set; drop the built-in one.
            help_command=None,
        )
        self.cog_folder = cog_folder
        # Casefolded names of roles that may use the admin commands, in addition
        # to Manage Server. Read by lib.permissions.is_admin via interaction.client.
        self.admin_roles: set[str] = admin_roles or set()
        # Created in setup_hook (needs a running event loop) and shared by every
        # cog via ``self.bot.session``; closed in ``close()``.
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        """One-time async startup, run by discord.py before connecting.

        Opens the HTTP session, loads all cogs, and syncs the slash-command
        tree with Discord.
        """
        self.session = aiohttp.ClientSession()
        # Route every uncaught app-command exception through our handler below.
        self.tree.on_error = self.on_application_command_error

        # Auto-discover cogs: load every .py file in the cogs folder. This means
        # dropping a new cog file in is enough to register it -- no central list
        # to keep in sync.
        for filename in os.listdir(self.cog_folder):
            if filename.endswith(".py"):
                await self.load_extension(f"{self.cog_folder}.{filename[:-3]}")
                _log.info("Loaded cog: %s", filename)

        # Push the current set of slash commands to Discord. A global sync can
        # take a little while to propagate to clients, but only needs to happen
        # when the command set changes.
        try:
            synced = await self.tree.sync()
            _log.info("Synced %d command(s) globally", len(synced))
        except discord.HTTPException as e:
            # A failed sync isn't fatal -- the previously-synced commands keep
            # working; log it and carry on rather than crashing on startup.
            _log.error("Command sync failed (keeping existing set): %s", e)

    async def close(self) -> None:
        """Clean shutdown: close our HTTP session before the base class closes."""
        if self.session is not None:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        """Fired once the gateway connection is established; just logs identity."""
        _log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handle errors from (nonexistent) prefix commands.

        Because ``command_prefix`` is an @-mention, simply mentioning the bot
        can trigger command parsing that finds nothing. Swallow that specific
        "not found" case quietly and re-raise anything genuinely unexpected.
        """
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Global handler for slash-command failures.

        Maps known error types to specific user-facing messages and falls back
        to a generic message (plus a logged traceback) for anything unexpected.
        Every reply is ephemeral so errors never clutter the channel.
        """
        # discord.py wraps exceptions raised inside a command in
        # CommandInvokeError with the real cause on ``.original``; unwrap it for
        # logging so the log shows the actual failure, not the wrapper.
        original = getattr(error, "original", error)

        if isinstance(error, NotAdmin):
            # Raised by lib.permissions.is_admin on the admin commands.
            message = (
                "You need the **Manage Server** permission or an authorized role "
                "to use this command."
            )
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission to use this command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"This command is on cooldown. Try again in {int(error.retry_after)}s."
        else:
            # Anything we didn't anticipate: show a generic message to the user
            # and log the full context (which command, who, where) with the
            # traceback so it's debuggable.
            message = "❌ Something went wrong talking to tadoku.app. Try again in a moment."
            command = interaction.command
            command_name = command.name if command is not None else "<no-command>"
            _log.error(
                "Unhandled error in command %r (guild=%s user=%s): %s",
                command_name,
                interaction.guild_id,
                getattr(interaction.user, "id", "?"),
                original,
                exc_info=error,
            )

        # Deliver the message on whichever channel is still open: if the command
        # already sent/deferred a response we must use followup, otherwise the
        # initial response slot.
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException:
            # If even the error reply fails (e.g. the interaction expired),
            # there's nothing more to do but record it.
            _log.exception("Failed to deliver error message to Discord")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Catch-all for exceptions in non-command event handlers; log and continue."""
        _log.exception("Ignoring exception in %s", event_method)
