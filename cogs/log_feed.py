"""Log-feed cog: the ``/log`` command group and the poller behind it.

``/log on channel:#x`` (Manage Server) turns on a live feed: every minute the
bot reads the server's current contest's event feed on tadoku.app and keeps the
chosen channel in line with it. A newly submitted log is posted — who logged
it, what they logged, and the points — as an embed "card"; an edited log has
its card edited in place; a withdrawn or deleted log has its card deleted.

If the logger has linked their Discord account via ``/claim``, the log posts as a
rendered image **profile card** (see ``lib.profile_card``): their Discord avatar,
their place and score in the current contest (as the card's subtitle), their
stats for that contest (characters, pages, listening hours — summed live from
tadoku.app's per-user log history, restricted to the contest's date window), and
this log. Everyone else gets the plain embed card. An edit re-renders the whole
card, so it shows the log's and the logger's current numbers.

Any log whose description contains HTTP(S) URLs gets them posted beneath the
card as clickable links. NSFW-tagged logs suppress previews on that follow-up.
The links message follows edits too (sent, edited, or deleted as needed).

After the logs, the poller posts a **"moved up" card** (see ``lib.rank_card``) for
anyone those logs pushed up the contest's top ``RANK_CALLOUT_DEPTH`` — overtaking
a rival, or breaking into the slice from below. It compares the live top slice
against a per-guild snapshot from the previous poll; the first baseline (or a
change of pinned contest) only seeds the snapshot, so no stale callouts fire.

Per guild the poller keeps a ``stream`` (see ``config_store.get_guild_logfeed``):
its cursor in the contest's event feed, and which Discord messages belong to
which log. Each event fetches its log's *current* state, so the event type only
decides whether a new post is allowed (a submission) — replaying an event edits
the card it already posted instead of posting it twice. The cursor is seeded to
"now" (no backlog dump) and saved after every event, so a restart or a
tadoku.app failure resumes exactly where it stopped. ``_poll_guild`` holds the
testable core; the ``tasks.loop`` just drives it.
"""

import functools
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

# Safety cap on event-feed pages (100 events each) read per guild per poll. The
# cursor is saved after every event, so stopping early loses nothing -- the next
# poll carries on from there. This just bounds a long outage catch-up (or a feed
# that keeps answering has_more) so one guild can't stall the poller.
EVENT_PAGES_PER_POLL = 10

# Most new logs to post in a single poll. The cursor stops before the next one,
# so a burst drains over the following polls instead of flooding the channel.
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


# Tags that make a Reading "Page" log a comic page, mirroring tadoku's scoring
# rules (which lowercase and trim tags before matching).
COMIC_TAGS = {"comic", "manga"}


def _is_comic(log: dict) -> bool:
    return any(str(tag).strip().casefold() in COMIC_TAGS for tag in log.get("tags") or [])


async def compute_window_totals(session, user_id: str, start, end) -> dict:
    """Sum a user's logs in the half-open window ``[start, end)`` into characters /
    pages / comic pages / listening minutes.

    Buckets each non-deleted log by its tracking data. Amount-based logs use their
    unit: "character" -> characters, "page" ("Page") -> pages, and Listening
    "minute" ("Minute"/"Dense minute") -> listening minutes. Comic pages are a
    Reading "Page" log tagged comic/manga (tadoku's logging v2 scores them by tag),
    plus pre-v2 logs in the retired "Comic page" unit. Newer time-tracked Listening
    logs instead expose ``duration_seconds`` with no meaningful amount/unit, which
    is converted to minutes. If a Listening log contains both forms, duration wins
    so the same session is not counted twice. Comic is checked before ``page``
    since both are "page" logs. Logs arrive newest-first: any log at/after
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
            elif "comic" in unit or (  # before the plain "page" check
                unit == "page" and activity in ("", "reading") and _is_comic(log)
            ):
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


# HTTP(S) URLs, excluding whitespace and common markup delimiters.
_URL_RE = re.compile(r'''https?://[^\s<>"']+''', re.IGNORECASE)


def _description_urls(log: dict) -> list[str]:
    """Extract clickable HTTP(S) URLs in order, regardless of media tags."""
    urls = []
    for url in _URL_RE.findall(log.get("description") or ""):
        url = url.rstrip(".,;:!?。 、")
        # Remove prose/Markdown closing brackets, preserving balanced URL parts
        # such as Wikipedia's /Title_(novel).
        while url and url[-1] in ")]}":
            closing = url[-1]
            opening = {")": "(", "]": "[", "}": "{"}[closing]
            if url.count(closing) <= url.count(opening):
                break
            url = url[:-1]
        urls.append(url)
    return urls


def _is_nsfw(log: dict) -> bool:
    return any(str(tag).strip().casefold() == "nsfw" for tag in log.get("tags") or [])


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
        """Bring one guild's feed channel in line with its contest's new events.

        Pulled out of the loop so tests drive it directly. With no stream yet, a
        stream for a different contest, or a cursor cleared by ``/log on``, this
        poll only seeds the cursor at "now" and posts nothing. Otherwise it works
        through the events from the saved cursor (``_apply_events``), then posts
        any rank-up cards those logs earned.
        """
        settings = config_store.get_guild_logfeed(guild_id)
        if not settings["enabled"] or not settings["channel_id"]:
            return

        try:
            contest = await leaderboard._resolve_contest(self.bot, guild_id)
        except tadoku.TadokuAPIError:
            _log.warning("Log feed for guild %s: tadoku.app lookup failed; will retry", guild_id)
            return

        stream = settings["stream"] or {}
        same_contest = stream.get("contest_id") == contest["id"]
        if not same_contest or stream.get("cursor") is None:
            # A new contest starts with an empty message map; the old contest's
            # cards stay as they are.
            messages = stream.get("messages", {}) if same_contest else {}
            await self._seed_stream(guild_id, contest["id"], messages)
            return

        # Claim map for this guild: a claimed logger gets the rendered profile
        # card (avatar + contest stats); everyone else gets the plain embed. The
        # caches memoise per-user avatar/stats lookups across a burst.
        claims = config_store.get_guild_claims(guild_id)
        avatar_cache: dict[int, Optional[bytes]] = {}
        render = functools.partial(
            self._message_for, contest=contest, claims=claims, avatar_cache=avatar_cache,
            stats_cache={}, poster_cache={}, standing_cache={},
        )
        changed = await self._apply_events(
            guild_id, settings["channel_id"], contest["id"], stream, render
        )

        # After the logs, spotlight anyone those logs pushed up the leaderboard.
        await self._post_rank_changes(guild_id, contest, changed, settings, claims, avatar_cache)

    async def _seed_stream(self, guild_id: int, contest_id: str, messages: dict) -> None:
        """Start following ``contest_id`` from its latest event (no backlog)."""
        try:
            page = await tadoku.get_contest_events(self.bot.session, contest_id, cursor="now")
        except tadoku.TadokuAPIError:
            _log.warning("Log feed for guild %s: couldn't start the event feed; will retry", guild_id)
            return
        config_store.set_guild_logfeed(guild_id, stream={
            "contest_id": contest_id, "cursor": page["next_cursor"], "messages": messages,
        })

    async def _apply_events(
        self, guild_id: int, channel_id: int, contest_id: str, stream: dict, render
    ) -> list[dict]:
        """Apply the contest's events after ``stream``'s cursor, in feed order.

        For each event the log's current state decides what happens to its
        Discord messages (``_sync_log``); only a ``log.submitted`` event may
        create a post, so updates to logs never posted here (e.g. from before
        ``/log on``) are skipped without a lookup. The stream -- cursor and
        message map -- is saved after every event. A tadoku.app error stops
        right before the failed event, keeping the cursor, so the next poll
        retries it; a rejected cursor is logged, never reset (that would skip
        events). Returns the logs whose cards were posted or edited.
        """
        messages = stream["messages"]
        changed: list[dict] = []
        posted = 0
        try:
            for _ in range(EVENT_PAGES_PER_POLL):
                page = await tadoku.get_contest_events(
                    self.bot.session, contest_id, cursor=stream["cursor"]
                )
                for event in page["events"]:
                    log_id = event["log_id"]
                    entry = messages.get(log_id)
                    if entry is None and event["event"] != "log.submitted":
                        pass  # nothing posted for this log, and only a submission posts
                    elif entry is None and posted >= MAX_POSTS_PER_POLL:
                        return changed  # the rest of the burst waits for the next poll
                    else:
                        log = await tadoku.get_contest_log(self.bot.session, contest_id, log_id)
                        synced = await self._sync_log(channel_id, entry, log, render)
                        if synced is None:
                            messages.pop(log_id, None)
                        else:
                            if entry is None:
                                posted += 1
                            messages[log_id] = synced
                            changed.append(log)
                    stream["cursor"] = event["id"]
                    config_store.set_guild_logfeed(guild_id, stream=stream)
                if not page["has_more"]:
                    break
        except tadoku.TadokuAPIError as e:
            _log.warning("Log feed for guild %s: %s; will retry", guild_id, e)
        return changed

    async def _sync_log(
        self, channel_id: int, entry: Optional[dict], log: Optional[dict], render
    ) -> Optional[dict]:
        """Make one log's Discord messages match its current state.

        ``log`` is ``None`` when the log isn't in the contest (any more): its
        messages are deleted. With no ``entry`` yet the log is posted; otherwise
        its card (and links message) are edited in place. Returns the log's
        message entry ``{"channel_id", "card", "links"}``, or ``None`` when
        nothing is posted for it (any more).
        """
        if log is None:
            if entry is not None:
                await self._delete_log_messages(entry)
            return None
        if entry is None:
            return await self._post_log(channel_id, log, render)
        return await self._edit_log_messages(entry, log, render)

    async def _post_log(self, channel_id: int, log: dict, render) -> Optional[dict]:
        """Post a log's card, plus its description links beneath it."""
        channel = await self._channel(channel_id)
        if channel is None:
            return None
        card = await self._send(channel, **await render(log))
        if card is None:
            return None
        # Make links clickable even when the description is drawn in an image.
        links = await self._sync_links(channel, None, log)
        return {"channel_id": channel_id, "card": card.id, "links": links}

    async def _edit_log_messages(self, entry: dict, log: dict, render) -> Optional[dict]:
        """Re-render a posted log's card in place and sync its links message.

        Passing both ``embed`` and ``attachments`` lets a card switch between the
        plain embed and the image card (e.g. the logger ``/claim``ed since). A
        card someone deleted by hand takes its links message with it and the log
        is no longer followed, rather than reposted.
        """
        channel = await self._channel(entry["channel_id"])
        if channel is None:
            return entry  # unreachable right now; keep following the log
        message = await render(log)
        try:
            await channel.get_partial_message(entry["card"]).edit(
                embed=message.get("embed"),
                attachments=[message["file"]] if "file" in message else [],
            )
        except discord.NotFound:
            await self._delete_log_messages(entry)
            return None
        except discord.HTTPException:
            _log.warning("Log feed: couldn't edit message %s", entry["card"])
            return entry
        return {**entry, "links": await self._sync_links(channel, entry.get("links"), log)}

    async def _sync_links(self, channel, links_id: Optional[int], log: dict) -> Optional[int]:
        """Send, edit or delete the links message under a card to match the log's
        description URLs; return its id (``None`` when there are no links)."""
        urls = _description_urls(log)
        if links_id is None:
            if not urls:
                return None
            sent = await self._send(channel, content="\n".join(urls), suppress_embeds=_is_nsfw(log))
            return sent.id if sent else None
        try:
            if not urls:
                await channel.get_partial_message(links_id).delete()
                return None
            # Only a full Message can toggle embed suppression on edit.
            message = await channel.fetch_message(links_id)
            await message.edit(content="\n".join(urls), suppress=_is_nsfw(log))
        except discord.NotFound:
            return None  # deleted by hand; don't bring it back
        except discord.HTTPException:
            _log.warning("Log feed: couldn't update links message %s", links_id)
        return links_id

    async def _delete_log_messages(self, entry: dict) -> None:
        """Delete a log's card and links message (already-gone ones are fine)."""
        channel = await self._channel(entry["channel_id"])
        if channel is None:
            return
        for message_id in (entry["card"], entry.get("links")):
            if message_id is None:
                continue
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                _log.warning("Log feed: couldn't delete message %s", message_id)

    async def _post_rank_changes(
        self, guild_id, contest, logs, settings, claims, avatar_cache
    ) -> None:
        """Post a "moved up" card for anyone this poll's posted or edited ``logs``
        pushed up the top slice.

        Compares the contest's current top ``RANK_CALLOUT_DEPTH`` against the
        snapshot kept from the previous poll. A logger from this poll who climbed
        past a rival -- or broke into the slice from below -- gets one card, capped
        at ``MAX_RANK_CALLOUTS`` (best moves first). The first baseline (or a change
        of pinned contest) only seeds the snapshot, with no callouts. A lookup
        failure is swallowed so a hiccup here never disturbs the log feed itself.
        """
        logger_ids = {log.get("user_id") for log in logs if log.get("user_id")}
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

    async def _channel(self, channel_id: int):
        """Resolve a channel (cache, then a live fetch), or ``None`` if it's gone."""
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                _log.warning("Log feed: channel %s not found", channel_id)
                return None
        return channel

    async def _send(
        self,
        channel,
        content: Optional[str] = None,
        embed: Optional[discord.Embed] = None,
        file: Optional[discord.File] = None,
        suppress_embeds: bool = False,
    ) -> Optional[discord.Message]:
        """Send a message (text, embed and/or file); ``None`` if Discord refused."""
        try:
            return await channel.send(content, embed=embed, file=file, suppress_embeds=suppress_embeds)
        except discord.HTTPException:
            _log.warning("Log feed: couldn't post to channel %s", channel.id)
            return None

    async def _post(self, channel_id: int, **message) -> None:
        """Send a message to the channel, tolerating a missing/forbidden one."""
        channel = await self._channel(channel_id)
        if channel is not None:
            await self._send(channel, **message)

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
        settings = config_store.get_guild_logfeed(interaction.guild_id)
        fields = {"enabled": True, "channel_id": target.id}
        if not settings["enabled"] and settings["stream"]:
            # Back on after a pause: the next poll restarts from "now" (no backlog
            # dump) but keeps the message map, so cards already posted still follow
            # their logs. Already on (e.g. moving channels): keep its place.
            fields["stream"] = {**settings["stream"], "cursor": None}
        config_store.set_guild_logfeed(interaction.guild_id, **fields)
        await interaction.response.send_message(
            f"✅ Live log feed is **on** in {target.mention}. New logs in this server's contest "
            f"will appear here within about a minute, and edits or deletions on tadoku.app "
            f"update their cards.",
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
