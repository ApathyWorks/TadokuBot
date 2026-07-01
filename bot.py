import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

_log = logging.getLogger(__name__)


class TadokuBot(commands.Bot):
    def __init__(self, cog_folder: str = "cogs"):
        # Read-only, slash-command-only bot: no message content or member
        # intents needed, so no privileged-intent approval is required.
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.cog_folder = cog_folder
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        self.tree.on_error = self.on_application_command_error

        for filename in os.listdir(self.cog_folder):
            if filename.endswith(".py"):
                await self.load_extension(f"{self.cog_folder}.{filename[:-3]}")
                _log.info("Loaded cog: %s", filename)

        try:
            synced = await self.tree.sync()
            _log.info("Synced %d command(s) globally", len(synced))
        except discord.HTTPException as e:
            _log.error("Command sync failed (keeping existing set): %s", e)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        _log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission to use this command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"This command is on cooldown. Try again in {int(error.retry_after)}s."
        else:
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

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException:
            _log.exception("Failed to deliver error message to Discord")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        _log.exception("Ignoring exception in %s", event_method)
