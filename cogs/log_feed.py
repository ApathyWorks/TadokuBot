"""Log-feed cog: the ``/log`` command group and the poller behind it.

``/log on channel:#x`` (Manage Server) turns on a live feed: every minute the
bot checks the server's current contest for new logs on tadoku.app and posts each
one — who logged it, what they logged, and the points — to the chosen channel as
an embed "card". If the logger has linked their Discord account via ``/claim``,
the card carries their Discord avatar.

If the logger has linked their Discord account via ``/claim``, the log posts as a
rendered image **profile card** (see ``lib.profile_card``): their Discord avatar,
their immersion stats since the start of 2026 (characters, pages, listening hours
— summed live from tadoku.app's per-user log history), and this log. Everyone
else gets the plain embed card.

The poller keeps a per-guild ``last_seen`` high-water mark (the ``created_at`` of
the newest log already posted) so it never repeats a log or dumps a backlog: on
enable the mark is seeded to "now", and each poll posts only logs newer than it.
``_poll_guild`` holds the testable core; the ``tasks.loop`` just drives it.
"""

import io
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import cogs.leaderboard as leaderboard
import lib.config_store as config_store
import lib.poster_client as poster_client
import lib.profile_card as profile_card
import lib.tadoku_client as tadoku
from lib.permissions import is_admin

_log = logging.getLogger(__name__)

# How often to poll tadoku.app for new logs.
POLL_INTERVAL_MINUTES = 1

# Page size for fetching logs (the API caps this at 100).
LOG_PAGE_SIZE = 100

# Safety cap on pages scanned per guild per poll. At a 1-minute cadence far fewer
# than 100 new logs arrive, so page 0 almost always suffices; this just bounds a
# pathological burst / a long outage catch-up.
LOGFEED_MAX_PAGES = 5

# Most logs to post in a single poll before collapsing the remainder into an
# "…and N more" trailer, so a burst can't flood the channel.
MAX_POSTS_PER_POLL = 20

# Safety cap on pages walked when summing a user's log history for the profile
# stats. 50 x 100 = 5,000 logs covers any realistic member; it just bounds the
# pathological case (and the cost, since this runs per claimed logger).
LIFETIME_MAX_PAGES = 50

# The profile card's stats are summed from this date forward (not truly all-time):
# everything logged since the start of 2026, accumulating going ahead.
LIFETIME_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

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


def _this_log_line(log: dict) -> str:
    """The one-line "what they just logged" for the profile card's callout:
    activity, amount + unit, and points (no language -- it lives off the card)."""
    activity = _activity_name(log)
    amount = _format_points(log.get("amount", 0))
    unit = log.get("unit_name", "")
    points = _format_points(log.get("score", 0))
    what = f"{amount} {unit}".strip()
    parts = [p for p in (activity, what) if p]
    parts.append(f"+{points} pts")
    return "  ·  ".join(parts)


def _format_log_embed(log: dict, avatar_url: Optional[str] = None) -> discord.Embed:
    """Render one log as a plain embed card: who, what, and points.

    Used for loggers who haven't linked their Discord account (claimed loggers
    get the richer rendered image card instead), and as a fallback when the
    lifetime lookup fails. ``avatar_url`` is shown beside the name when known.
    """
    activity = _activity_name(log)
    emoji = _ACTIVITY_EMOJI.get(activity, "📝")
    amount = _format_points(log.get("amount", 0))
    unit = log.get("unit_name", "")
    language = _language_name(log)
    name = (log.get("user_display_name") or "Someone").strip()
    points = _format_points(log.get("score", 0))

    embed = discord.Embed(
        # e.g. "📖 Reading"; keep the emoji alone if the activity name is missing.
        title=f"{emoji} {activity}".strip(),
        color=_ACTIVITY_COLOR.get(activity, discord.Color.blurple()),
    )
    embed.set_author(name=name, icon_url=avatar_url)

    # A title (the log's description) is optional -- show it quoted when present.
    description = (log.get("description") or "").strip()
    if description:
        embed.description = f"「{description}」"

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

        # Claim map for this guild: a claimed logger gets the rendered profile
        # card (avatar + lifetime stats); everyone else gets the plain embed. The
        # caches memoise per-user avatar/lifetime lookups across a burst.
        claims = config_store.get_guild_claims(guild_id)
        avatar_cache: dict[int, Optional[bytes]] = {}
        lifetime_cache: dict[str, Optional[dict]] = {}
        poster_cache: dict[tuple, Optional[bytes]] = {}
        for log in to_post:
            message = await self._message_for(
                log, claims, avatar_cache, lifetime_cache, poster_cache
            )
            await self._post(settings["channel_id"], **message)
        if overflow > 0:
            await self._post(
                settings["channel_id"],
                content=f"…and {overflow} more log(s) in the last few minutes.",
            )

        config_store.set_guild_logfeed(guild_id, last_seen=newest_created_at)

    async def _message_for(
        self,
        log: dict,
        claims: dict[str, str],
        avatar_cache: dict[int, Optional[bytes]],
        lifetime_cache: dict[str, Optional[dict]],
        poster_cache: dict[tuple, Optional[bytes]],
    ) -> dict:
        """Build the ``send`` kwargs for one log.

        A claimed logger whose lifetime stats we can fetch gets the rendered image
        profile card (``file=``) -- the material title is drawn on the card itself,
        and when the log is tagged with a media type we recognise (anime/manga/
        game/book) its cover is drawn on the right. Everyone else -- and a claimed
        logger whose lifetime lookup fails -- gets the plain embed card.
        """
        name = (log.get("user_display_name") or "Someone").strip()
        claimer = _claimer_id(claims, name)
        if claimer is not None:
            lifetime = await self._lifetime_stats(log.get("user_id"), lifetime_cache)
            if lifetime is not None:
                avatar_bytes = await self._avatar_bytes_for_id(claimer, avatar_cache)
                poster_bytes = await self._poster_bytes_for(log, poster_cache)
                try:
                    png = await profile_card.render_card(
                        display_name=name,
                        subtitle="Immersion since 2026",
                        avatar_bytes=avatar_bytes,
                        characters=lifetime["characters"],
                        pages=lifetime["pages"],
                        listening_hours=lifetime["minutes"] / 60,
                        this_log=_this_log_line(log),
                        # The material title now lives on the card (in the log callout).
                        title=(log.get("description") or "").strip(),
                        # A cover for the tagged material, drawn on the right (or None).
                        poster_bytes=poster_bytes,
                    )
                    return {"file": discord.File(io.BytesIO(png), filename="log.png")}
                except Exception:  # noqa: BLE001 -- a render failure must never freeze
                    # the whole guild's feed; degrade to the plain embed for this log.
                    _log.exception("Log feed: profile card render failed for %r; using embed", name)

        return {"embed": _format_log_embed(log)}

    async def _poster_bytes_for(
        self, log: dict, cache: dict[tuple, Optional[bytes]]
    ) -> Optional[bytes]:
        """Return a cover image for the log's material, or ``None``.

        Delegates to ``poster_client`` (which routes on the log's tags to MAL /
        VNDB / Google Books); any failure yields ``None`` so a missing cover never
        breaks the card. Results are memoised across a burst via ``cache``.
        """
        try:
            return await poster_client.fetch_poster(
                self.bot.session, log.get("tags"), log.get("description") or "", cache
            )
        except Exception:  # noqa: BLE001 -- a poster is optional; never fail the card
            _log.warning("Log feed: poster lookup failed for %r", log.get("description"))
            return None

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
        """Sum a user's logs since ``LIFETIME_START`` into characters / pages /
        listening minutes.

        Buckets each non-deleted log by its unit: anything with "character" in the
        unit name counts as characters, "page" as pages (covers "Comic page"), and
        "minute" as listening minutes ("Minute"/"Dense minute"). Logs arrive
        newest-first, so the first one before ``LIFETIME_START`` ends the walk
        (everything older is out of window). Pages the API by ``total_size``,
        bounded by ``LIFETIME_MAX_PAGES``.
        """
        characters = pages = minutes = 0.0
        for page in range(LIFETIME_MAX_PAGES):
            data = await tadoku.list_user_logs(
                self.bot.session, user_id, page=page, page_size=LOG_PAGE_SIZE
            )
            logs = data.get("logs", [])
            for log in logs:
                # Newest-first: a log before the window means we're done entirely.
                if leaderboard._parse_timestamp(log["created_at"]) < LIFETIME_START:
                    return {"characters": characters, "pages": pages, "minutes": minutes}
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

    async def _avatar_bytes_for_id(
        self, uid: int, cache: dict[int, Optional[bytes]]
    ) -> Optional[bytes]:
        """Return the PNG/other bytes of user ``uid``'s Discord avatar, or ``None``.

        Resolves the user (member cache, then a live ``fetch_user``) and reads
        their avatar asset. Any failure yields ``None`` (the card then renders a
        placeholder disc). Results (including misses) are memoised so a burst from
        one person costs at most one download.
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
        try:
            data = await user.display_avatar.read()
        except discord.HTTPException:
            data = None
        cache[uid] = data
        return data

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
        file: Optional[discord.File] = None,
    ) -> None:
        """Send a message (text, embed and/or file) to the channel, tolerating a
        missing/forbidden one."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                _log.warning("Log feed: channel %s not found", channel_id)
                return
        try:
            await channel.send(content, embed=embed, file=file)
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
            f"will appear here within about a minute.",
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
