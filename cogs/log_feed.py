"""Log-feed cog: the ``/log`` command group and the poller behind it.

``/log on channel:#x`` (Manage Server) turns on a live feed: every 5 minutes the
bot checks the server's current contest for new logs on tadoku.app and posts each
one — who logged it, what they logged, and the points — to the chosen channel.

The poller keeps a per-guild ``last_seen`` high-water mark (the ``created_at`` of
the newest log already posted) so it never repeats a log or dumps a backlog: on
enable the mark is seeded to "now", and each poll posts only logs newer than it.
``_poll_guild`` holds the testable core; the ``tasks.loop`` just drives it.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import cogs.leaderboard as leaderboard
import lib.config_store as config_store
import lib.tadoku_client as tadoku

_log = logging.getLogger(__name__)

# How often to poll tadoku.app for new logs.
POLL_INTERVAL_MINUTES = 5

# Page size for fetching logs (the API caps this at 100).
LOG_PAGE_SIZE = 100

# Safety cap on pages scanned per guild per poll. At a 5-minute cadence far fewer
# than 100 new logs arrive, so page 0 almost always suffices; this just bounds a
# pathological burst / a long outage catch-up.
LOGFEED_MAX_PAGES = 5

# Most logs to post in a single poll before collapsing the remainder into an
# "…and N more" trailer, so a burst can't flood the channel.
MAX_POSTS_PER_POLL = 20

# Emoji per activity name, with a neutral fallback.
_ACTIVITY_EMOJI = {"Reading": "📖", "Listening": "🎧"}


def _format_points(score) -> str:
    """Render a score without a pointless trailing ``.0`` (192, 7.2, 3)."""
    return f"{score:.1f}".rstrip("0").rstrip(".")


def _activity_name(log: dict) -> str:
    activity = log.get("activity")
    return activity.get("name", "") if isinstance(activity, dict) else (activity or "")


def _language_name(log: dict) -> str:
    language = log.get("language")
    return language.get("name", "") if isinstance(language, dict) else (language or "")


def _format_log(log: dict) -> str:
    """Render one log as a single feed line: who, what, and points."""
    activity = _activity_name(log)
    emoji = _ACTIVITY_EMOJI.get(activity, "📝")
    amount = _format_points(log.get("amount", 0))
    unit = log.get("unit_name", "")
    language = _language_name(log)
    lang_part = f" ({language})" if language else ""

    # A title (the log's description) is optional -- show it quoted when present.
    description = (log.get("description") or "").strip()
    title_part = f" — 「{description}」" if description else ""

    name = (log.get("user_display_name") or "Someone").strip()
    return (
        f"{emoji} **{name}** logged "
        f"**{amount} {unit}** · {activity}{lang_part}{title_part} · "
        f"**+{_format_points(log.get('score', 0))} pts**"
    )


class LogFeed(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Start the poller when the cog is added (not in __init__, so simply
        constructing the cog in tests doesn't spin up a live loop)."""
        self.poll_logs.start()

    async def cog_unload(self) -> None:
        self.poll_logs.cancel()

    # -- poller -------------------------------------------------------------

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_logs(self) -> None:
        await self._run_poll()

    @poll_logs.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_poll(self) -> None:
        """Poll every guild with the feed configured; isolate failures."""
        for guild_id in config_store.guilds_with_logfeed():
            try:
                await self._poll_guild(guild_id)
            except Exception:  # noqa: BLE001 -- one bad guild mustn't stop the rest
                _log.exception("Log feed poll failed for guild %s", guild_id)

    async def _poll_guild(self, guild_id: int) -> None:
        """Post any logs newer than ``last_seen`` for one guild, then advance it.

        Pulled out of the loop so tests drive it directly. On a tadoku.app failure
        we leave ``last_seen`` untouched so the next tick retries the same window.
        """
        settings = config_store.get_guild_logfeed(guild_id)
        if not settings["enabled"] or not settings["channel_id"]:
            return

        last_seen = settings["last_seen"]
        # Seeded to "now" on enable, so this is only None for legacy/edge cases;
        # treat a missing mark as "post nothing yet" by seeding to now.
        if last_seen is None:
            config_store.set_guild_logfeed(
                guild_id, last_seen=datetime.now(timezone.utc).isoformat()
            )
            return
        cutoff = leaderboard._parse_timestamp(last_seen)

        try:
            contest = await leaderboard._resolve_contest(self.bot, guild_id)
            new_logs = await self._collect_new_logs(contest["id"], cutoff)
        except tadoku.TadokuAPIError:
            _log.warning("Log feed for guild %s: tadoku.app lookup failed; will retry", guild_id)
            return

        if not new_logs:
            return

        # ``_collect_new_logs`` returns newest-first; the newest is the new mark.
        newest_created_at = new_logs[0]["created_at"]
        # Post oldest -> newest so the channel reads chronologically.
        to_post = list(reversed(new_logs))
        overflow = len(to_post) - MAX_POSTS_PER_POLL
        if overflow > 0:
            # Keep the most recent ones (the tail of the chronological list).
            to_post = to_post[-MAX_POSTS_PER_POLL:]

        for log in to_post:
            await self._post(settings["channel_id"], _format_log(log))
        if overflow > 0:
            await self._post(
                settings["channel_id"], f"…and {overflow} more log(s) in the last few minutes."
            )

        config_store.set_guild_logfeed(guild_id, last_seen=newest_created_at)

    async def _collect_new_logs(self, contest_id: str, cutoff: datetime) -> list[dict]:
        """Return non-deleted logs with ``created_at`` after ``cutoff``, newest-first.

        Logs arrive newest-first, so we stop at the first log at/older than the
        cutoff (everything after is older too) or at a short page, bounded by
        ``LOGFEED_MAX_PAGES``.
        """
        collected: list[dict] = []
        for page in range(LOGFEED_MAX_PAGES):
            logs = await tadoku.list_contest_logs(
                self.bot.session, contest_id, page=page, page_size=LOG_PAGE_SIZE
            )
            for log in logs:
                if leaderboard._parse_timestamp(log["created_at"]) <= cutoff:
                    return collected  # reached logs we've already seen
                if not log.get("deleted"):
                    collected.append(log)
            if len(logs) < LOG_PAGE_SIZE:
                break
        return collected

    async def _post(self, channel_id: int, content: str) -> None:
        """Send ``content`` to the channel, tolerating a missing/forbidden one."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                _log.warning("Log feed: channel %s not found", channel_id)
                return
        try:
            await channel.send(content)
        except discord.HTTPException:
            _log.warning("Log feed: couldn't post to channel %s", channel_id)

    # -- commands -----------------------------------------------------------

    log_group = app_commands.Group(
        name="log",
        description="Live feed of new contest logs to a channel.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @log_group.command(name="on", description="Start posting new contest logs to a channel.")
    @app_commands.describe(channel="Channel to post new logs in (defaults to this channel).")
    async def log_on(
        self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
    ):
        """Enable the live log feed, posting to ``channel`` (default: here)."""
        target = channel or interaction.channel
        if not target.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                f"❌ I don't have permission to send messages in {target.mention}.",
                ephemeral=True,
            )
            return
        # Seed the mark to now so only logs from here on are posted (no backlog).
        config_store.set_guild_logfeed(
            interaction.guild_id,
            enabled=True,
            channel_id=target.id,
            last_seen=datetime.now(timezone.utc).isoformat(),
        )
        await interaction.response.send_message(
            f"✅ Live log feed is **on** in {target.mention}. New logs in this server's contest "
            f"will appear here within ~{POLL_INTERVAL_MINUTES} minutes.",
            ephemeral=True,
        )

    @log_group.command(name="off", description="Stop the live log feed for this server.")
    async def log_off(self, interaction: discord.Interaction):
        """Disable the live log feed (leaves the contest pin untouched)."""
        config_store.set_guild_logfeed(interaction.guild_id, enabled=False)
        await interaction.response.send_message(
            "✅ Live log feed is now **off**.", ephemeral=True
        )

    @log_group.command(name="status", description="Show whether the live log feed is on, and where.")
    async def log_status(self, interaction: discord.Interaction):
        """Report whether the feed is enabled and which channel it posts to."""
        settings = config_store.get_guild_logfeed(interaction.guild_id)
        if settings["enabled"] and settings["channel_id"]:
            await interaction.response.send_message(
                f"The live log feed is **on**, posting to <#{settings['channel_id']}>.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "The live log feed is **off**. Use `/log on` to enable it.", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(LogFeed(bot))
