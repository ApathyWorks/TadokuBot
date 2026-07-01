from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

import lib.config_store as config_store
import lib.tadoku_client as tadoku

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
PAGE_SIZE = 15

ACTIVITY_CHOICES = [
    Choice(name="Reading", value=1),
    Choice(name="Listening", value=2),
]


async def _resolve_contest(bot: commands.Bot, guild_id: Optional[int]) -> dict:
    """Returns the full contest detail this guild is configured to show,
    falling back to the latest official contest if nothing is configured
    (or if there's no guild, e.g. a DM)."""
    configured = config_store.get_guild_contest(guild_id) if guild_id else None
    if configured:
        return await tadoku.get_contest(bot.session, configured["contest_id"])
    return await tadoku.get_latest_official_contest(bot.session)


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _language_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[Choice[str]]:
        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
        except tadoku.TadokuAPIError:
            return []

        languages = contest.get("allowed_languages") or []
        current = current.lower()
        matches = [
            lang for lang in languages
            if current in lang.get("code", "").lower() or current in lang.get("name", "").lower()
        ]
        return [
            Choice(name=f"{lang['name']} ({lang['code']})", value=lang["code"])
            for lang in matches[:25]
        ]

    @app_commands.command(
        name="leaderboard",
        description="Show this server's tadoku.app contest leaderboard.",
    )
    @app_commands.describe(
        page="Page number, starting at 1 (default 1).",
        language="Optional: only show entries for this language.",
        activity="Optional: only show entries for this activity type.",
    )
    @app_commands.autocomplete(language=_language_autocomplete)
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 10_000] = 1,
        language: Optional[str] = None,
        activity: Optional[Choice[int]] = None,
    ):
        await interaction.response.defer()

        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
            data = await tadoku.get_contest_leaderboard(
                self.bot.session,
                contest["id"],
                page=page - 1,
                page_size=PAGE_SIZE,
                language_code=language,
                activity_id=activity.value if activity else None,
            )
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        entries = data.get("entries", [])
        if not entries:
            await interaction.followup.send(
                f"No leaderboard entries on page {page} for **{contest['title']}**."
            )
            return

        lines = []
        for entry in entries:
            rank = entry["rank"]
            marker = MEDALS.get(rank, f"`#{rank:>3}`")
            tie = " *(tie)*" if entry.get("is_tie") else ""
            lines.append(f"{marker} {entry['user_display_name']} — {entry['score']:.1f}{tie}")

        filters = []
        if language:
            filters.append(f"language: {language}")
        if activity:
            filters.append(f"activity: {activity.name}")
        filter_note = f" ({', '.join(filters)})" if filters else ""

        embed = discord.Embed(
            title=f"🏆 {contest['title']}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"{contest['contest_start']} – {contest['contest_end']} · "
                f"Page {page} · {data.get('total_size', len(entries))} participants{filter_note}"
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
