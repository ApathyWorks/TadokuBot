"""Log-feed cog: the ``/log`` command group and the poller behind it.

``/log on channel:#x`` (Manage Server) turns on a live feed: every minute the
bot checks the server's current contest for new logs on tadoku.app and posts each
one — who logged it, what they logged, and the points — to the chosen channel as
an embed "card". If the logger has linked their Discord account via ``/claim``,
the card carries their Discord avatar.

If the logger has linked their Discord account via ``/claim``, the log posts as a
rendered image **profile card** (see ``lib.profile_card``): their Discord avatar,
their place and score in the current contest (as the card's subtitle), their
stats for that contest (characters, pages, listening hours — summed live from
tadoku.app's per-user log history, restricted to the contest's date window), and
this log. Everyone else gets the plain embed card.

A ``youtube``-tagged log whose description contains URL(s) also gets them posted
as a follow-up message beneath the card, so Discord renders a playable preview
for each.

After the logs, the poller posts a **"moved up" card** (see ``lib.rank_card``) for
anyone those logs pushed up the contest's top ``RANK_CALLOUT_DEPTH`` — overtaking
a rival, or breaking into the slice from below. It compares the live top slice
against a per-guild snapshot from the previous poll; the first baseline (or a
change of pinned contest) only seeds the snapshot, so no stale callouts fire.

The poller keeps a per-guild ``last_seen`` high-water mark (the ``created_at`` of
the newest log already posted) so it never repeats a log or dumps a backlog: on
enable the mark is seeded to "now", and each poll posts only logs newer than it.
``_poll_guild`` holds the testable core; the ``tasks.loop`` just drives it.
"""

import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import cogs.leaderboard as leaderboard
import lib.config_store as config_store
import lib.poster_client as poster_client
import lib.profile_card as profile_card
import lib.rank_card as rank_card
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
CONTEST_LOG_MAX_PAGES = 50

# Rank-change callouts: how deep into the leaderboard a move is worth announcing
# (a pass or a break-in below this rank isn't news), and a per-poll cap so a big
# reshuffle can't flood the channel with "moved up" cards.
RANK_CALLOUT_DEPTH = 20
MAX_RANK_CALLOUTS = 3

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


def _format_minutes(minutes: float) -> str:
    """Render a duration with a singular/plural minute label."""
    value = _format_points(minutes)
    label = "minute" if value == "1" else "minutes"
    return f"{value} {label}"


def _format_tracking(log: dict) -> str:
    """Render every tracked value in a log.

    Legacy logs carry ``amount`` + ``unit_name`` (for example ``30 Minute``).
    Tadoku's time-based tracker instead carries ``duration_seconds`` and leaves
    the unit empty. A Listening log may legally contain both minute forms; show
    only the larger value (preferring duration on a tie) so one listening session
    is not presented twice. Other activities preserve both values because their
    amount and duration describe different measurements.
    """
    values = []
    unit = (log.get("unit_name") or "").strip()
    amount = log.get("amount") or 0
    duration_seconds = log.get("duration_seconds")
    duration_minutes = duration_seconds / 60 if duration_seconds is not None else None

    if (
        _activity_name(log).strip().casefold() == "listening"
        and "minute" in unit.casefold()
        and duration_minutes is not None
    ):
        if duration_minutes >= amount:
            return _format_minutes(duration_minutes)
        return f"{_format_points(amount)} {unit}"

    if unit:
        values.append(f"{_format_points(amount)} {unit}")
    if duration_seconds is not None:
        values.append(_format_minutes(duration_minutes))
    return " · ".join(values)


def _contest_window(contest: dict) -> tuple[datetime, datetime]:
    """Return the ``[start, end)`` UTC datetimes spanning ``contest``'s days.

    ``contest_start``/``contest_end`` are inclusive calendar dates (e.g.
    "2026-07-01"); both are normalised to midnight UTC and the end is widened to
    the following midnight, so a log made any time on the contest's final day
    still falls inside the half-open window.
    """
    def _midnight(value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    return _midnight(contest["contest_start"]), _midnight(contest["contest_end"]) + timedelta(days=1)


async def compute_window_totals(session, user_id: str, start, end) -> dict:
    """Sum a user's logs in the half-open window ``[start, end)`` into characters /
    pages / comic pages / listening minutes.

    Buckets each non-deleted log by its tracking data. Amount-based logs use their
    unit: "character" -> characters, "comic" ("Comic page") -> comic_pages,
    "page" ("Page") -> pages, and Listening "minute" ("Minute"/"Dense minute")
    -> listening minutes. Newer time-tracked Listening logs instead expose
    ``duration_seconds`` with no meaningful amount/unit, which is converted to
    minutes. If a Listening log contains both forms, duration wins so the same
    session is not counted twice. ``comic`` is checked before ``page`` since
    "Comic page" contains "page". Logs arrive newest-first: any log at/after
    ``end`` is skipped (a later window bound), and the first one before ``start``
    ends the walk (everything before it is out of window too). ``end`` may be
    ``None`` for an open-ended window (up to now). Pages the API by ``total_size``,
    bounded by ``CONTEST_LOG_MAX_PAGES``. Shared by the contest card and the
    weekly/monthly cards.
    """
    characters = pages = comic_pages = minutes = 0.0

    def _totals():
        return {"characters": characters, "pages": pages,
                "comic_pages": comic_pages, "minutes": minutes}

    for page in range(CONTEST_LOG_MAX_PAGES):
        data = await tadoku.list_user_logs(session, user_id, page=page, page_size=LOG_PAGE_SIZE)
        logs = data.get("logs", [])
        for log in logs:
            ts = leaderboard._parse_timestamp(log["created_at"])
            # Newest-first: logs after the window aren't counted (but older ones
            # may still lie inside it, so keep scanning)...
            if end is not None and ts >= end:
                continue
            # ...and the first log before the window ends the walk entirely.
            if ts < start:
                return _totals()
            if log.get("deleted"):
                continue
            unit = (log.get("unit_name") or "").lower()
            amount = log.get("amount") or 0
            activity = _activity_name(log).strip().casefold()
            duration_seconds = log.get("duration_seconds")
            if activity == "listening" and duration_seconds is not None:
                minutes += duration_seconds / 60
            elif "character" in unit:
                characters += amount
            elif "comic" in unit:  # "Comic page" -- before the plain "page" check
                comic_pages += amount
            elif "page" in unit:
                pages += amount
            elif "minute" in unit and activity in ("", "listening"):
                # Missing activity keeps compatibility with older API payloads;
                # an explicit Speaking/Study minute must not inflate Listening.
                minutes += amount
        # Stop at a short page or once we've covered the reported total.
        if len(logs) < LOG_PAGE_SIZE or (page + 1) * LOG_PAGE_SIZE >= data.get("total_size", 0):
            break
    return _totals()


async def compute_contest_totals(session, user_id: str, contest: dict) -> dict:
    """Sum a user's logs within ``contest``'s date window (see ``compute_window_totals``)."""
    start, end = _contest_window(contest)
    return await compute_window_totals(session, user_id, start, end)


def format_standing(entry: dict, contest: dict) -> str:
    """The card subtitle for a leaderboard ``entry``: place, score and contest.

    Renders e.g. ``"#5 · 320.5 pts in 2026 Round 4"`` (with a "(tie)" marker when
    the rank is shared). Shared by the log-feed card and the ``/card`` command.
    """
    tie = " (tie)" if entry.get("is_tie") else ""
    return f"#{entry['rank']}{tie} · {_format_points(entry['score'])} pts in {contest['title']}"


def _activity_name(log: dict) -> str:
    activity = log.get("activity")
    return activity.get("name", "") if isinstance(activity, dict) else (activity or "")


def _language_name(log: dict) -> str:
    language = log.get("language")
    return language.get("name", "") if isinstance(language, dict) else (language or "")


def _claimer_id(claims: dict[str, str], name: str) -> Optional[int]:
    """Return the Discord id that claimed ``name`` (via /claim), or ``None``.

    Folds names the same way /claim does, so a leaderboard spelling like "Ruby "
    matches a claim stored as "ruby".
    """
    target = leaderboard._normalize_name(name)
    for uid, claimed in claims.items():
        if leaderboard._normalize_name(claimed) == target:
            return int(uid)
    return None


def _rank_callouts(entries: list[dict], old_ranks: dict, logger_ids: set, depth: int) -> list[dict]:
    """The top-slice entries a poll's logs pushed up, each with a "what they did" note.

    ``entries`` is the current top slice (rank order); ``old_ranks`` maps user id
    to previous rank in that slice; ``logger_ids`` are the people who logged this
    poll; ``depth`` is the slice size (for the "broke into the top N" wording). A
    mover is an entry that logged this poll and either climbed (new rank below its
    old one) or is newly in the slice (no old rank). Returns them as entry dicts
    with an added ``description``, best move (lowest new rank) first.
    """
    by_rank = {entry["rank"]: entry for entry in entries}
    movers = []
    for entry in entries:
        uid = entry.get("user_id")
        if uid not in logger_ids:
            continue
        new_rank = entry["rank"]
        old_rank = old_ranks.get(uid)
        if old_rank is None:
            description = f"Broke into the top {depth}"
        elif new_rank < old_rank:
            passed = old_rank - new_rank
            below = by_rank.get(new_rank + 1)  # the rival now directly beneath them
            if below is None:
                description = f"Climbed {passed} place{'s' if passed != 1 else ''}"
            elif passed == 1:
                description = f"Passed {below['user_display_name']}"
            else:
                description = f"Passed {below['user_display_name']} and {passed - 1} other{'s' if passed - 1 != 1 else ''}"
        else:
            continue  # same rank or dropped -> not news
        movers.append({**entry, "description": description})
    movers.sort(key=lambda entry: entry["rank"])
    return movers


# First http(s) URL in a string (stops at whitespace).
_URL_RE = re.compile(r"https?://\S+")


def _youtube_urls(log: dict) -> list[str]:
    """Every URL in a YouTube-tagged log's description (in order), or ``[]``.

    Only fires when the log carries a ``youtube`` tag, so the feed can post the
    link(s) under the card (Discord renders a playable preview for each). Any
    trailing punctuation Discord would choke on is left as-is -- log URLs are
    pasted, not prose.
    """
    tags = {str(t).lower() for t in (log.get("tags") or [])}
    if "youtube" not in tags:
        return []
    return _URL_RE.findall(log.get("description") or "")


def _format_tags(log: dict) -> str:
    """The log's tags as hashtag tokens ("#fiction #game"), or "" if it has none.

    Keeps the order the logger picked them in and de-dupes case-insensitively, so
    the tags read on the card the way they were entered on tadoku.app.
    """
    seen: set[str] = set()
    tokens = []
    for tag in (log.get("tags") or []):
        text = str(tag).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            tokens.append(f"#{text}")
    return " ".join(tokens)


def _this_log_line(log: dict) -> str:
    """The one-line "what they just logged" for the profile card's callout: the
    logger's tags, then activity, tracked amount and/or duration, and points (no
    language -- it lives off the card)."""
    activity = _activity_name(log)
    points = _format_points(log.get("score", 0))
    what = _format_tracking(log)
    tags = _format_tags(log)
    parts = [p for p in (tags, activity, what) if p]
    parts.append(f"+{points} pts")
    return "  ·  ".join(parts)


def _format_log_embed(log: dict, avatar_url: Optional[str] = None) -> discord.Embed:
    """Render one log as a plain embed card: who, what, and points.

    Used for loggers who haven't linked their Discord account (claimed loggers
    get the richer rendered image card instead), and as a fallback when the
    contest-stats lookup fails. ``avatar_url`` is shown beside the name when known.
    """
    activity = _activity_name(log)
    emoji = _ACTIVITY_EMOJI.get(activity, "📝")
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

    embed.add_field(name="Amount", value=_format_tracking(log) or "—", inline=True)
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
        # card (avatar + contest stats); everyone else gets the plain embed. The
        # caches memoise per-user avatar/stats lookups across a burst.
        claims = config_store.get_guild_claims(guild_id)
        avatar_cache: dict[int, Optional[bytes]] = {}
        stats_cache: dict[str, Optional[dict]] = {}
        poster_cache: dict[tuple, Optional[bytes]] = {}
        standing_cache: dict[str, str] = {}
        for log in to_post:
            message = await self._message_for(
                log, contest, claims, avatar_cache, stats_cache,
                poster_cache, standing_cache,
            )
            await self._post(settings["channel_id"], **message)
            # For a YouTube log, drop the video link(s) under the card in one
            # follow-up message so Discord renders a playable preview for each.
            urls = _youtube_urls(log)
            if urls:
                await self._post(settings["channel_id"], content="\n".join(urls))
        if overflow > 0:
            await self._post(
                settings["channel_id"],
                content=f"…and {overflow} more log(s) in the last few minutes.",
            )

        # After the logs, spotlight anyone those logs pushed up the leaderboard.
        await self._post_rank_changes(
            guild_id, contest, new_logs, settings, claims, avatar_cache
        )

        config_store.set_guild_logfeed(guild_id, last_seen=newest_created_at)

    async def _post_rank_changes(
        self, guild_id, contest, new_logs, settings, claims, avatar_cache
    ) -> None:
        """Post a "moved up" card for anyone this poll's logs pushed up the top slice.

        Compares the contest's current top ``RANK_CALLOUT_DEPTH`` against the
        snapshot kept from the previous poll. A logger from this poll who climbed
        past a rival -- or broke into the slice from below -- gets one card, capped
        at ``MAX_RANK_CALLOUTS`` (best moves first). The first baseline (or a change
        of pinned contest) only seeds the snapshot, with no callouts. A lookup
        failure is swallowed so a hiccup here never disturbs the log feed itself.
        """
        logger_ids = {log.get("user_id") for log in new_logs if log.get("user_id")}
        if not logger_ids:
            return
        try:
            data = await tadoku.get_contest_leaderboard(
                self.bot.session, contest["id"], page=0, page_size=RANK_CALLOUT_DEPTH
            )
        except tadoku.TadokuAPIError:
            _log.warning("Log feed for guild %s: rank-change lookup failed", guild_id)
            return

        entries = data.get("entries", [])
        new_ranks = {entry["user_id"]: entry["rank"] for entry in entries if entry.get("user_id")}
        snapshot = settings.get("rank_snapshot") or {}
        # Only compare against a snapshot for the *same* contest; otherwise seed.
        old_ranks = snapshot["ranks"] if snapshot.get("contest_id") == contest["id"] else None
        config_store.set_guild_logfeed(
            guild_id, rank_snapshot={"contest_id": contest["id"], "ranks": new_ranks}
        )
        if old_ranks is None:
            return

        for entry in _rank_callouts(entries, old_ranks, logger_ids, RANK_CALLOUT_DEPTH)[:MAX_RANK_CALLOUTS]:
            claimer = _claimer_id(claims, entry["user_display_name"])
            avatar = await self._avatar_bytes_for_id(claimer, avatar_cache) if claimer is not None else None
            card = rank_card.RankChangeCard(
                name=entry["user_display_name"],
                rank=entry["rank"],
                description=entry["description"],
                footer=contest["title"],
                avatar=avatar,
            )
            try:
                png = await rank_card.render(card)
            except Exception:  # noqa: BLE001 -- a render failure mustn't sink the feed
                _log.exception("Rank-change card render failed for %r", entry["user_display_name"])
                continue
            await self._post(
                settings["channel_id"], file=discord.File(io.BytesIO(png), filename="rankup.png")
            )

    async def _message_for(
        self,
        log: dict,
        contest: dict,
        claims: dict[str, str],
        avatar_cache: dict[int, Optional[bytes]],
        stats_cache: dict[str, Optional[dict]],
        poster_cache: dict[tuple, Optional[bytes]],
        standing_cache: dict[str, str],
    ) -> dict:
        """Build the ``send`` kwargs for one log.

        A claimed logger whose contest stats we can fetch gets the rendered image
        profile card (``file=``) -- the material title is drawn on the card itself,
        the card's subtitle shows their place and score in ``contest``, and when
        the log is tagged with a media type we recognise (anime/manga/game/book)
        its cover is drawn on the right. Everyone else -- and a claimed logger whose
        stats lookup fails -- gets the plain embed card.
        """
        name = (log.get("user_display_name") or "Someone").strip()
        claimer = _claimer_id(claims, name)
        if claimer is not None:
            stats = await self._contest_stats(log.get("user_id"), contest, stats_cache)
            if stats is not None:
                avatar_bytes = await self._avatar_bytes_for_id(claimer, avatar_cache)
                poster_bytes = await self._poster_bytes_for(log, poster_cache)
                subtitle = await self._contest_standing(contest, name, standing_cache)
                try:
                    png = await profile_card.render_card(
                        display_name=name,
                        subtitle=subtitle,
                        avatar_bytes=avatar_bytes,
                        characters=stats["characters"],
                        pages=stats["pages"],
                        comic_pages=stats["comic_pages"],
                        listening_hours=stats["minutes"] / 60,
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

    async def _contest_standing(
        self, contest: dict, display_name: str, cache: dict[str, str]
    ) -> str:
        """Return the card subtitle: the logger's place and score in ``contest``.

        Looks ``display_name`` up on the contest's cumulative leaderboard and
        renders it as e.g. ``"#5 · 320.5 pts in 2026 Round 4"`` (with a "(tie)"
        marker when the rank is shared). Returns "" when they aren't on the
        leaderboard yet or tadoku.app can't be reached, so a missing standing just
        drops the subtitle rather than failing the card. Memoised per display name
        across a burst via ``cache``.
        """
        key = leaderboard._normalize_name(display_name)
        if key in cache:
            return cache[key]
        try:
            entry = await leaderboard._find_leaderboard_entry(
                self.bot, contest["id"], display_name
            )
        except tadoku.TadokuAPIError:
            _log.warning("Log feed: leaderboard lookup for %r failed", display_name)
            entry = None
        standing = "" if entry is None else format_standing(entry, contest)
        cache[key] = standing
        return standing

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

    async def _contest_stats(
        self, user_id: Optional[str], contest: dict, cache: dict[str, Optional[dict]]
    ) -> Optional[dict]:
        """Return ``{characters, pages, comic_pages, minutes}`` for a user's logs in
        ``contest``, or ``None``.

        ``None`` when the id is missing or tadoku.app can't be reached. Results
        (including misses) are memoised in ``cache`` so a burst from one person
        costs at most one walk of their history.
        """
        if not user_id:
            return None
        if user_id in cache:
            return cache[user_id]
        try:
            stats = await self._compute_contest_totals(user_id, contest)
        except tadoku.TadokuAPIError:
            _log.warning("Log feed: contest-stats lookup for user %s failed", user_id)
            stats = None
        cache[user_id] = stats
        return stats

    async def _compute_contest_totals(self, user_id: str, contest: dict) -> dict:
        """Instance wrapper around ``compute_contest_totals`` (uses ``self.bot``)."""
        return await compute_contest_totals(self.bot.session, user_id, contest)

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
