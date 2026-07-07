"""Log-feed cog: the ``/log`` command group and the poller behind it.

``/log on channel:#x`` (Manage Server) turns on a live feed: every 5 minutes the
bot checks the server's current contest for new logs on tadoku.app and posts each
one — who logged it, what they logged, and the points — to the chosen channel as
an embed "card". If the logger has linked their Discord account via ``/claim``,
the card carries their Discord avatar.

If the logger has linked their Discord account via ``/claim``, the log posts as a
richer "profile card": their Discord avatar, their all-time immersion stats
(characters, pages, listening hours — summed live from tadoku.app's per-user log
history) on the left, and this log on the right. Everyone else gets the plain log
card.

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
from lib.permissions import is_admin

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

# Safety cap on pages walked when summing a user's whole log history for the
# lifetime stats. 50 x 100 = 5,000 logs covers any realistic member; it just
# bounds the pathological case (and the cost, since this runs per claimed logger).
LIFETIME_MAX_PAGES = 50

# Emoji per activity name, with a neutral fallback.
_ACTIVITY_EMOJI = {"Reading": "📖", "Listening": "🎧"}

# Accent colour per activity, so the cards are visually distinguishable at a
# glance; anything unrecognised falls back to blurple.
_ACTIVITY_COLOR = {
    "Reading": discord.Color.blue(),
    "Listening": discord.Color.purple(),
}


def _format_points(score) -> str:
    """Render a score without a pointless trailing ``.0`` (192, 7.2, 3)."""
    return f"{score:.1f}".rstrip("0").rstrip(".")


def _format_count(n: float) -> str:
    """Human-readable count: 6,600,000 -> "6.6M", 12,300 -> "12.3k", 812 -> "812"."""
    n = int(round(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def _activity_name(log: dict) -> str:
    activity = log.get("activity")
    return activity.get("name", "") if isinstance(activity, dict) else (activity or "")


def _language_name(log: dict) -> str:
    language = log.get("language")
    return language.get("name", "") if isinstance(language, dict) else (language or "")


def _claimer_id(claims: dict[str, str], name: str) -> Optional[int]:
    """Return the Discord id that claimed ``name`` (via /claim), or ``None``.

    Folds names the same way /score and /claim do, so a leaderboard spelling like
    "Ruby " matches a claim stored as "ruby".
    """
    target = leaderboard._normalize_name(name)
    for uid, claimed in claims.items():
        if leaderboard._normalize_name(claimed) == target:
            return int(uid)
    return None


def _lifetime_value(lifetime: dict) -> str:
    """The left-column body: a claimed member's all-time totals."""
    hours = lifetime.get("minutes", 0) / 60
    return (
        f"**Characters** {_format_count(lifetime.get('characters', 0))}\n"
        f"**Pages** {_format_count(lifetime.get('pages', 0))}\n"
        f"**Listening** {hours:.1f}h"
    )


def _this_log_value(log: dict) -> str:
    """The right-column body: what this particular log is."""
    amount = _format_points(log.get("amount", 0))
    unit = log.get("unit_name", "")
    language = _language_name(log)
    points = _format_points(log.get("score", 0))
    lines = [f"**{f'{amount} {unit}'.strip()}**"]
    if language:
        lines.append(language)
    lines.append(f"**+{points}** pts")
    return "\n".join(lines)


def _format_log_embed(
    log: dict, avatar_url: Optional[str] = None, lifetime: Optional[dict] = None
) -> discord.Embed:
    """Render one log as an embed "card": who, what, and points.

    Carries the same information as the old one-line text (logger, activity,
    amount + unit, language, optional material title, points). ``avatar_url``
    (the logger's Discord avatar, when they've claimed the username) is shown
    beside their name.

    When ``lifetime`` is given (a claimed member's all-time totals) the card
    becomes a two-column "profile" layout — their immersion stats on the left,
    this log on the right — like a proper card. Without it, the log details sit
    in the usual Amount / Language / Points fields.
    """
    activity = _activity_name(log)
    emoji = _ACTIVITY_EMOJI.get(activity, "📝")
    language = _language_name(log)
    name = (log.get("user_display_name") or "Someone").strip()

    embed = discord.Embed(
        # e.g. "📖 Reading"; keep the emoji alone if the activity name is missing.
        title=f"{emoji} {activity}".strip(),
        color=_ACTIVITY_COLOR.get(activity, discord.Color.blurple()),
    )
    # The logger's identity, with their Discord avatar when linked via /claim.
    embed.set_author(name=name, icon_url=avatar_url)

    # A title (the log's description) is optional -- show it quoted when present.
    description = (log.get("description") or "").strip()
    if description:
        embed.description = f"「{description}」"

    if lifetime is not None:
        # Two inline fields sit side by side: lifetime stats | this log.
        embed.add_field(name="📊 Immersion", value=_lifetime_value(lifetime), inline=True)
        embed.add_field(name=f"{emoji} This log", value=_this_log_value(log), inline=True)
    else:
        amount = _format_points(log.get("amount", 0))
        unit = log.get("unit_name", "")
        points = _format_points(log.get("score", 0))
        embed.add_field(name="Amount", value=f"{amount} {unit}".strip() or "—", inline=True)
        if language:
            embed.add_field(name="Language", value=language, inline=True)
        embed.add_field(name="Points", value=f"+{points}", inline=True)
    return embed


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

        # Claim map for this guild: a claimed logger gets the rich profile card
        # (avatar + lifetime stats); everyone else gets the plain log card. The
        # caches memoise per-user avatar/lifetime lookups across a burst.
        claims = config_store.get_guild_claims(guild_id)
        avatar_cache: dict[int, Optional[str]] = {}
        lifetime_cache: dict[str, Optional[dict]] = {}
        for log in to_post:
            embed = await self._build_card(log, claims, avatar_cache, lifetime_cache)
            await self._post(settings["channel_id"], embed=embed)
        if overflow > 0:
            await self._post(
                settings["channel_id"],
                content=f"…and {overflow} more log(s) in the last few minutes.",
            )

        config_store.set_guild_logfeed(guild_id, last_seen=newest_created_at)

    async def _build_card(
        self,
        log: dict,
        claims: dict[str, str],
        avatar_cache: dict[int, Optional[str]],
        lifetime_cache: dict[str, Optional[dict]],
    ) -> discord.Embed:
        """Build the embed for one log: a rich profile card if the logger has
        claimed their username, else the plain log card.

        A claimed logger always gets their avatar; the lifetime stats column is
        added when the (live) lifetime lookup succeeds -- a tadoku.app hiccup just
        drops the column, it never blocks the post.
        """
        name = (log.get("user_display_name") or "Someone").strip()
        claimer = _claimer_id(claims, name)
        if claimer is None:
            return _format_log_embed(log)

        avatar_url = await self._avatar_url_for_id(claimer, avatar_cache)
        lifetime = await self._lifetime_stats(log.get("user_id"), lifetime_cache)
        return _format_log_embed(log, avatar_url=avatar_url, lifetime=lifetime)

    async def _lifetime_stats(
        self, user_id: Optional[str], cache: dict[str, Optional[dict]]
    ) -> Optional[dict]:
        """Return a user's all-time ``{characters, pages, minutes}``, or ``None``.

        ``None`` when the id is missing or tadoku.app can't be reached. Results
        (including misses) are memoised in ``cache`` so a burst from one person
        costs at most one walk of their history.
        """
        if not user_id:
            return None
        if user_id in cache:
            return cache[user_id]
        try:
            stats = await self._compute_lifetime(user_id)
        except tadoku.TadokuAPIError:
            _log.warning("Log feed: lifetime lookup for user %s failed", user_id)
            stats = None
        cache[user_id] = stats
        return stats

    async def _compute_lifetime(self, user_id: str) -> dict:
        """Sum a user's whole log history into characters / pages / listening minutes.

        Buckets each non-deleted log by its unit: anything with "character" in the
        unit name counts as characters, "page" as pages (covers "Comic page"), and
        "minute" as listening minutes ("Minute"/"Dense minute"). Pages the API by
        ``total_size``, bounded by ``LIFETIME_MAX_PAGES``.
        """
        characters = pages = minutes = 0.0
        for page in range(LIFETIME_MAX_PAGES):
            data = await tadoku.list_user_logs(
                self.bot.session, user_id, page=page, page_size=LOG_PAGE_SIZE
            )
            logs = data.get("logs", [])
            for log in logs:
                if log.get("deleted"):
                    continue
                unit = (log.get("unit_name") or "").lower()
                amount = log.get("amount") or 0
                if "character" in unit:
                    characters += amount
                elif "page" in unit:
                    pages += amount
                elif "minute" in unit:
                    minutes += amount
            # Stop at a short page or once we've covered the reported total.
            if len(logs) < LOG_PAGE_SIZE or (page + 1) * LOG_PAGE_SIZE >= data.get("total_size", 0):
                break
        return {"characters": characters, "pages": pages, "minutes": minutes}

    async def _avatar_url_for_id(
        self, uid: int, cache: dict[int, Optional[str]]
    ) -> Optional[str]:
        """Return the Discord avatar URL for user ``uid``, or ``None`` if it can't
        be resolved.

        Tries the cache, then the bot's member cache, then a live ``fetch_user``.
        Results (including misses) are memoised so a burst from one person costs
        at most one lookup.
        """
        if uid in cache:
            return cache[uid]
        user = self.bot.get_user(uid)
        if user is None:
            try:
                user = await self.bot.fetch_user(uid)
            except discord.HTTPException:
                cache[uid] = None
                return None
        url = user.display_avatar.url
        cache[uid] = url
        return url

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

    async def _post(
        self,
        channel_id: int,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        """Send a message (text and/or embed) to the channel, tolerating a
        missing/forbidden one."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                _log.warning("Log feed: channel %s not found", channel_id)
                return
        try:
            await channel.send(content, embed=embed)
        except discord.HTTPException:
            _log.warning("Log feed: couldn't post to channel %s", channel_id)

    # -- commands -----------------------------------------------------------

    # Access is enforced at runtime by is_admin() on each subcommand (Manage
    # Server or an ADMIN_ROLES role), not by static default_permissions.
    log_group = app_commands.Group(
        name="log",
        description="Live feed of new contest logs to a channel.",
        guild_only=True,
    )

    @log_group.command(name="on", description="Start posting new contest logs to a channel.")
    @app_commands.describe(channel="Channel to post new logs in (defaults to this channel).")
    @is_admin()
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
    @is_admin()
    async def log_off(self, interaction: discord.Interaction):
        """Disable the live log feed (leaves the contest pin untouched)."""
        config_store.set_guild_logfeed(interaction.guild_id, enabled=False)
        await interaction.response.send_message(
            "✅ Live log feed is now **off**.", ephemeral=True
        )

    @log_group.command(name="status", description="Show whether the live log feed is on, and where.")
    @is_admin()
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
