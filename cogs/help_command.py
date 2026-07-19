"""Help cog: the ``/tadokubot`` and ``/tadokutag`` commands + the online announcement.

``/tadokubot`` shows everyone a grouped list of the bot's commands (general vs
Manage-Server-only); ``/tadokutag`` explains which tadoku.app tag to put on a log
so its cover (or link) shows on the log-feed card. When the bot first connects,
it also posts a short "I'm online — run /tadokubot" pointer to each server's
existing bot channel (its log feed channel, or failing that its alerts channel),
so members discover the command list without an admin having to advertise it.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import lib.config_store as config_store

_log = logging.getLogger(__name__)

# The bot's source repository, linked from /tadokubot.
REPO_URL = "https://github.com/ApathyWorks/TadokuBot"

# The command catalogue rendered by /tadokubot, split by who can use each.
# (name, one-line description). Kept as data so the embed and the "no command
# drifts out of the list" test share one source of truth.
GENERAL_COMMANDS = [
    ("/tadokubot", "Show this list of commands."),
    ("/tadokutag", "How to tag a log so its cover / link shows on the card."),
    ("/leaderboard", "This server's contest leaderboard."),
    ("/score", "Look up one person's rank and score in the contest."),
    ("/weeklyleaderboard", "Ranking of points logged in the last 7 days."),
    ("/monthlyleaderboard", "Ranking of points logged in a calendar month."),
    ("/current_contest", "Which contest this server is currently set to."),
    ("/claim", "Link your Discord account to your tadoku.app username."),
    ("/unclaim", "Remove the username you claimed."),
    ("/unclaimedlist", "List contest participants nobody has claimed yet."),
]

# Which tadoku.app tag makes each kind of material show a cover (or link) on the
# log card. (tag label, what it produces.) One source of truth for the embed.
MEDIA_TAGS = [
    ("vn", "Visual-novel cover (VNDB)"),
    ("game", "Game cover (VNDB, then Steam)"),
    ("anime", "Anime cover (MyAnimeList)"),
    ("manga", "Manga cover (MyAnimeList)"),
    ("book", "Book cover (Google Books)"),
    ("tv / movie / show", "Live-action film or TV cover (TMDB)"),
    ("youtube", "Posts the video link from the log's description under the card"),
]

ADMIN_COMMANDS = [
    ("/set_contest", "Pick which contest /leaderboard shows."),
    ("/shame", "Toggle the weekly \"logged nothing\" call-out."),
    ("/alerts on|off|status", "Automatic weekly / monthly / year-end leaderboard posts."),
    ("/log on|off|status", "Live feed of new contest logs to a channel."),
    ("/autoclaim", "Auto-link participants to same-named Discord members."),
]


def build_help_embed() -> discord.Embed:
    """Render the grouped command list as an embed."""
    embed = discord.Embed(
        title="🤖 TadokuBot commands",
        # Linking the title makes it clickable straight to the repo.
        url=REPO_URL,
        description="Leaderboards and stats from tadoku.app for this server's contest.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Commands",
        value="\n".join(f"**{name}** — {desc}" for name, desc in GENERAL_COMMANDS),
        inline=False,
    )
    embed.add_field(
        name="Admin (Manage Server)",
        value="\n".join(f"**{name}** — {desc}" for name, desc in ADMIN_COMMANDS),
        inline=False,
    )
    embed.add_field(name="Source", value=f"[GitHub]({REPO_URL})", inline=False)
    return embed


def build_tag_embed() -> discord.Embed:
    """Render the media-tag guide as an embed."""
    embed = discord.Embed(
        title="🏷️ Tagging your tadoku logs",
        description=(
            "When you log immersion on tadoku.app, add one of these **tags** so the "
            "bot can show the material's cover (or link) on your log card:"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Tag → what shows",
        value="\n".join(f"**{tag}** — {shows}" for tag, shows in MEDIA_TAGS),
        inline=False,
    )
    embed.add_field(
        name="Notes",
        value=(
            "• Covers appear on the **profile card** — link your account first with `/claim`.\n"
            "• For a **youtube** log, paste the video URL into the log's description.\n"
            "• No matching tag just means a plain card (no cover)."
        ),
        inline=False,
    )
    return embed


def _startup_channel_id(guild_id: int) -> int | None:
    """Where to post the online announcement for a guild, or ``None`` to skip.

    Reuses a channel the server already dedicates to the bot: the enabled log
    feed's channel first, then the enabled alerts channel. If neither is on we
    stay quiet rather than guessing at a channel.
    """
    feed = config_store.get_guild_logfeed(guild_id)
    if feed["enabled"] and feed["channel_id"]:
        return feed["channel_id"]
    # Alerts share one channel across kinds; the weekly entry is representative.
    alert = config_store.get_guild_alert(guild_id, "weekly")
    if alert["enabled"] and alert["channel_id"]:
        return alert["channel_id"]
    return None


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Guard so the announcement posts once per process, not on every
        # gateway reconnect (on_ready can fire many times).
        self._announced = False

    @app_commands.command(name="tadokubot", description="List everything TadokuBot can do.")
    async def tadokubot(self, interaction: discord.Interaction):
        """Show the grouped command list (ephemeral, so it never clutters chat)."""
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

    @app_commands.command(
        name="tadokutag",
        description="How to tag your tadoku logs so a cover or link shows on the card.",
    )
    async def tadokutag(self, interaction: discord.Interaction):
        """Explain the media tags that trigger a poster/link (ephemeral)."""
        await interaction.response.send_message(embed=build_tag_embed(), ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """On the first ready of this process, announce online to each guild."""
        if self._announced:
            return
        self._announced = True
        for guild in self.bot.guilds:
            try:
                await self._announce_to_guild(guild)
            except Exception:  # noqa: BLE001 -- one guild mustn't stop the rest
                _log.exception("Online announcement failed for guild %s", guild.id)

    async def _announce_to_guild(self, guild: discord.Guild) -> None:
        """Post the "I'm online" pointer to the guild's bot channel, if any."""
        channel_id = _startup_channel_id(guild.id)
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return
        try:
            await channel.send(
                "👋 **TadokuBot** is online! Run `/tadokubot` for the list of commands."
            )
        except discord.HTTPException:
            _log.warning("Online announcement: couldn't post to channel %s", channel_id)


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Help(bot))
