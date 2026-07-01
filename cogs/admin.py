"""Admin cog: commands for configuring which contest a server displays.

Two slash commands:
  * ``/set_contest`` -- pin this server's ``/leaderboard`` to a specific
    contest (Manage Server permission required), with name autocomplete.
  * ``/current_contest`` -- report which contest is currently pinned.

The per-server pin is persisted via ``lib.config_store``; everything else is
looked up live from tadoku.app.
"""

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
        """Autocomplete callback for the ``contest`` argument of ``/set_contest``.

        Fetches the contest list from tadoku.app and offers those whose title
        contains what the admin has typed so far. Each choice shows a
        human-readable label but carries the contest *id* as its value, so the
        command receives an unambiguous id rather than a name to re-resolve.
        """
        try:
            contests = await tadoku.list_contests(self.bot.session, page_size=50)
        except tadoku.TadokuAPIError:
            # Autocomplete must never error out the UI; on failure just offer
            # nothing and let the admin type again in a moment.
            return []

        # Case-insensitive substring match against the contest title.
        current = current.lower()
        matches = [c for c in contests if current in c.get("title", "").lower()]

        return [
            app_commands.Choice(
                # Label: "Title (start – end)", truncated to Discord's 100-char
                # limit for choice names.
                name=f"{c['title']} ({c['contest_start']} – {c['contest_end']})"[:100],
                # Value: the contest id the command actually needs.
                value=c["id"],
            )
            # Discord allows at most 25 autocomplete choices.
            for c in matches[:25]
        ]

    @app_commands.command(
        name="set_contest",
        description="Set which tadoku.app contest this server's /leaderboard shows.",
    )
    @app_commands.describe(contest="Start typing a contest name to search.")
    @app_commands.autocomplete(contest=_contest_autocomplete)
    # Only meaningful in a server (the pin is per-guild), never in DMs.
    @app_commands.guild_only()
    # Gate on Discord's own permission model -- only server managers may repin.
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_contest(self, interaction: discord.Interaction, contest: str):
        """Pin this server's leaderboard to the chosen contest id.

        ``contest`` is a contest id (supplied by the autocomplete above). We
        re-fetch the contest to (a) validate the id still exists and (b) get its
        canonical title to store and echo back.
        """
        # Defer ephemerally: the lookup is a network call, and the confirmation
        # only needs to be seen by the admin who ran it.
        await interaction.response.defer(ephemeral=True)
        try:
            contest_detail = await tadoku.get_contest(self.bot.session, contest)
        except tadoku.TadokuAPIError:
            # A 404 here means a stale/invalid id (e.g. typed by hand rather than
            # picked from autocomplete). Don't store anything.
            await interaction.followup.send(
                "❌ Couldn't find that contest on tadoku.app. Try picking one from the autocomplete list."
            )
            return

        # Persist the pin, storing the canonical title for later display.
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
        """Report the server's pinned contest, or the fallback behaviour if none.

        Read-only and available to everyone; the reply is ephemeral to avoid
        cluttering the channel.
        """
        configured = config_store.get_guild_contest(interaction.guild_id)
        if configured:
            # A contest is pinned -- name it (title comes straight from the store,
            # no API call needed).
            await interaction.response.send_message(
                f"This server's `/leaderboard` is set to **{configured['contest_title']}**. "
                f"Use `/set_contest` to change it.",
                ephemeral=True,
            )
        else:
            # Nothing pinned -- explain the latest-official fallback.
            await interaction.response.send_message(
                "No contest configured for this server yet — `/leaderboard` falls back to the "
                "latest official tadoku.app contest. Use `/set_contest` to pick a specific one.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Admin(bot))
