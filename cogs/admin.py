import discord
from discord import app_commands
from discord.ext import commands

import lib.config_store as config_store
import lib.tadoku_client as tadoku


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _contest_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            contests = await tadoku.list_contests(self.bot.session, page_size=50)
        except tadoku.TadokuAPIError:
            return []

        current = current.lower()
        matches = [c for c in contests if current in c.get("title", "").lower()]

        return [
            app_commands.Choice(
                name=f"{c['title']} ({c['contest_start']} – {c['contest_end']})"[:100],
                value=c["id"],
            )
            for c in matches[:25]
        ]

    @app_commands.command(
        name="set_contest",
        description="Set which tadoku.app contest this server's /leaderboard shows.",
    )
    @app_commands.describe(contest="Start typing a contest name to search.")
    @app_commands.autocomplete(contest=_contest_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_contest(self, interaction: discord.Interaction, contest: str):
        await interaction.response.defer(ephemeral=True)
        try:
            contest_detail = await tadoku.get_contest(self.bot.session, contest)
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't find that contest on tadoku.app. Try picking one from the autocomplete list."
            )
            return

        config_store.set_guild_contest(interaction.guild_id, contest_detail["id"], contest_detail["title"])
        await interaction.followup.send(
            f"✅ This server's `/leaderboard` now shows **{contest_detail['title']}**."
        )

    @app_commands.command(
        name="current_contest",
        description="Show which tadoku.app contest this server's /leaderboard is currently set to.",
    )
    @app_commands.guild_only()
    async def current_contest(self, interaction: discord.Interaction):
        configured = config_store.get_guild_contest(interaction.guild_id)
        if configured:
            await interaction.response.send_message(
                f"This server's `/leaderboard` is set to **{configured['contest_title']}**. "
                f"Use `/set_contest` to change it.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "No contest configured for this server yet — `/leaderboard` falls back to the "
                "latest official tadoku.app contest. Use `/set_contest` to pick a specific one.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
