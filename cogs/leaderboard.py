"""Leaderboard cog: the ``/leaderboard``, ``/weeklyleaderboard`` and
``/monthlyleaderboard`` commands.

They all resolve which contest this server should show (a pinned one, or the
latest official as a fallback). ``/leaderboard`` reads the contest's cumulative
ranking and renders it as a Discord embed; ``/weeklyleaderboard`` and
``/monthlyleaderboard`` instead tally raw logs over a window (the last 7 days, or
the current calendar month) to build the rolling/period rankings the API doesn't
expose directly, and render them as **image cards** (``lib.leaderboard_card``)
where the top three are drawn larger with medal badges. ``build_*_card`` assemble
the card model (shared with the scheduled alerts in ``cogs.alerts``, which also
render the year-end recap this way). ``build_daily_top_card`` builds the
end-of-day "top logger" spotlight (``lib.daily_card``) for the daily alert.

When a server has the shame setting on (the default; toggled via ``/shame``),
``/weeklyleaderboard`` and ``/monthlyleaderboard`` also append a "shame"
call-out naming anyone who has points in the contest overall but logged nothing
in that command's window.
"""

import io
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

import lib.config_store as config_store
import lib.daily_card as daily_card
import lib.leaderboard_card as leaderboard_card
import lib.tadoku_client as tadoku

# Emoji shown for the top three ranks; every other rank gets a plain "#N".
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# How many leaderboard rows to show per page/embed.
PAGE_SIZE = 15

# Page size used when scanning the leaderboard for a specific person. The API
# caps page_size at 100, so this is the largest (and thus fewest-requests) page.
LOOKUP_PAGE_SIZE = 100

# Safety cap on how many leaderboard pages a name lookup will scan before giving
# up. 50 pages of 100 covers 5,000 participants -- far beyond any real contest --
# and bounds the worst case so a typo can't trigger an unbounded request loop.
MAX_LOOKUP_PAGES = 50

# The rolling window /weeklyleaderboard tallies over.
WEEKLY_WINDOW_DAYS = 7

# Most names to spell out in the /weeklyleaderboard shame list before collapsing
# the rest into an "…and N more" tail (keeps the embed field within Discord's
# 1024-char limit and readable).
SHAME_LIST_LIMIT = 15

# Page size for fetching contest logs (the API caps this at 100).
LOG_PAGE_SIZE = 100

# Safety cap on log pages fetched for a period tally (weekly/monthly). Logs are
# newest-first and we stop as soon as we pass the window's cutoff, so in practice
# only the first few pages are read; this just bounds a pathological case. Set
# generously (1000 pages x 100 = 100k logs) so even a busy contest's full month
# is covered before the cap can undercount the oldest days.
MAX_LOG_PAGES = 1000

# The activity types the API supports, exposed as a fixed dropdown. The values
# are tadoku.app's activity ids (1 = reading, 2 = listening).
ACTIVITY_CHOICES = [
    Choice(name="Reading", value=1),
    Choice(name="Listening", value=2),
]

# Month dropdown for /monthlyleaderboard; value is the 1-based month number.
MONTH_CHOICES = [
    Choice(name="January", value=1),
    Choice(name="February", value=2),
    Choice(name="March", value=3),
    Choice(name="April", value=4),
    Choice(name="May", value=5),
    Choice(name="June", value=6),
    Choice(name="July", value=7),
    Choice(name="August", value=8),
    Choice(name="September", value=9),
    Choice(name="October", value=10),
    Choice(name="November", value=11),
    Choice(name="December", value=12),
]


async def _resolve_contest(bot: commands.Bot, guild_id: Optional[int]) -> dict:
    """Return the contest this guild's leaderboard should display.

    If the guild has pinned a contest via ``/set_contest`` we fetch that one;
    otherwise (including in DMs, where ``guild_id`` is ``None``) we fall back to
    the latest official contest.
    """
    configured = config_store.get_guild_contest(guild_id) if guild_id else None
    if configured:
        return await tadoku.get_contest(bot.session, configured["contest_id"])
    return await tadoku.get_latest_official_contest(bot.session)


def _normalize_name(name: str) -> str:
    """Fold a display name for comparison: trim surrounding whitespace and
    casefold. Tadoku display names sometimes carry trailing spaces (e.g.
    "ruby "), so a naive equality check would miss them."""
    return name.strip().casefold()


async def _find_leaderboard_entry(bot: commands.Bot, contest_id: str, display_name: str) -> Optional[dict]:
    """Scan a contest's leaderboard for a participant by display name.

    Pages through the leaderboard (100 at a time) and returns the first entry
    whose display name matches ``display_name`` case-insensitively, or ``None``
    if the person isn't on this leaderboard at all. Stops as soon as a match is
    found, at the last page, or at ``MAX_LOOKUP_PAGES`` -- whichever comes first.
    """
    target = _normalize_name(display_name)
    for page in range(MAX_LOOKUP_PAGES):
        data = await tadoku.get_contest_leaderboard(
            bot.session, contest_id, page=page, page_size=LOOKUP_PAGE_SIZE
        )
        entries = data.get("entries", [])
        for entry in entries:
            if _normalize_name(entry["user_display_name"]) == target:
                return entry
        # A short (or empty) page means we've reached the end of the leaderboard;
        # no point requesting further pages.
        if len(entries) < LOOKUP_PAGE_SIZE:
            break
    return None


def _parse_timestamp(value: str) -> datetime:
    """Parse a tadoku.app ISO-8601 timestamp into a timezone-aware datetime.

    The API returns UTC times with a trailing ``Z`` (e.g. "2026-07-01T22:56:46.1Z");
    swapping ``Z`` for ``+00:00`` makes ``fromisoformat`` produce a UTC-aware
    value that can be compared against the cutoff safely.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _logs_in_window(
    bot: commands.Bot,
    contest_id: str,
    cutoff: datetime,
    until: datetime | None = None,
) -> AsyncIterator[dict]:
    """Yield a contest's non-deleted logs created in the window ``[cutoff, until)``.

    Pages through the contest's logs (which arrive newest-first), so they're
    yielded newest-first too. Because of that ordering, the first log older than
    the cutoff means every remaining log is older too, so paging stops right
    there. Deleted logs are skipped.

    ``until`` is an optional exclusive upper bound: logs at or after it are
    skipped (used to scope to a window that ended before now -- a past calendar
    month, or yesterday for the daily alert). Left ``None`` the window is
    open-ended up to now. Since logs are newest-first, any too-new logs are seen
    and skipped before the in-window ones on the same pages.
    """
    for page in range(MAX_LOG_PAGES):
        logs = await tadoku.list_contest_logs(bot.session, contest_id, page=page, page_size=LOG_PAGE_SIZE)
        for log in logs:
            created = _parse_timestamp(log["created_at"])
            # Newest-first ordering: once we cross the cutoff we're done entirely.
            if created < cutoff:
                return
            # Newer than the window -- skip, but keep scanning back toward the cutoff.
            if until is not None and created >= until:
                continue
            if log.get("deleted"):
                continue
            yield log
        # A short/empty page is the end of the log history.
        if len(logs) < LOG_PAGE_SIZE:
            return


async def _tally_scores_since(
    bot: commands.Bot,
    contest_id: str,
    cutoff: datetime,
    until: datetime | None = None,
) -> dict[str, list]:
    """Sum each participant's log scores in the window ``[cutoff, until)``.

    See ``_logs_in_window`` for how the window is scanned. Returns
    ``{user_id: [display_name, total_score]}``; the display name is taken from
    each user's newest log in the window (the first one counted).
    """
    totals: dict[str, list] = {}
    async for log in _logs_in_window(bot, contest_id, cutoff, until):
        uid = log["user_id"]
        if uid not in totals:
            # First (newest) log for this user sets the display name.
            totals[uid] = [log.get("user_display_name", "Unknown"), 0.0]
        totals[uid][1] += log["score"]
    return totals


def _log_title(log: dict) -> str:
    """A log's material title for display: its description on one line, or
    "Untitled" when the logger left it blank."""
    return " ".join((log.get("description") or "").split()) or "Untitled"


async def _tally_day(
    bot: commands.Bot, contest_id: str, start: datetime, end: datetime
) -> dict[str, dict]:
    """Tally each participant's logs in ``[start, end)``, with a per-title breakdown.

    Returns ``{user_id: {"name", "score", "titles"}}``. ``titles`` maps a
    casefolded title to ``[title, points]`` so repeat logs of the same material
    (common: several sittings of one manga in a day) collapse into one line with
    their points summed. The display spellings -- of the person's name and each
    title -- come from the newest log, like ``_tally_scores_since``.
    """
    people: dict[str, dict] = {}
    async for log in _logs_in_window(bot, contest_id, start, end):
        person = people.setdefault(
            log["user_id"],
            {"name": log.get("user_display_name", "Unknown"), "score": 0.0, "titles": {}},
        )
        title = _log_title(log)
        person["titles"].setdefault(title.casefold(), [title, 0.0])[1] += log["score"]
        person["score"] += log["score"]
    return people


def _rank_by_score(totals: dict[str, list]) -> list[dict]:
    """Turn ``_tally_scores_since`` output into a ranked list of entry dicts.

    Sorts by total score descending and assigns standard competition ranks:
    users with an equal total share a rank (rank = 1 + how many scored strictly
    higher), and ``is_tie`` flags any total shared by more than one user.
    Each entry mirrors the leaderboard API shape
    (``rank``/``user_display_name``/``score``/``is_tie``) so rendering is uniform.
    """
    rows = sorted(totals.values(), key=lambda r: r[1], reverse=True)
    scores = [total for _, total in rows]
    ranked = []
    for name, total in rows:
        rank = 1 + sum(1 for s in scores if s > total)
        is_tie = scores.count(total) > 1
        ranked.append(
            {"rank": rank, "user_display_name": name, "score": total, "is_tie": is_tie}
        )
    return ranked


async def _scored_participants(bot: commands.Bot, contest_id: str) -> list[dict]:
    """Return every leaderboard entry with a positive cumulative score.

    Pages the contest's cumulative leaderboard (100 at a time). Because it's
    sorted by score descending, the first non-positive score means everyone
    after it also has zero, so we stop there; a short page ends the scan too.
    Bounded by ``MAX_LOOKUP_PAGES`` like the name-lookup scan so a huge contest
    can't trigger an unbounded request loop.
    """
    participants: list[dict] = []
    for page in range(MAX_LOOKUP_PAGES):
        data = await tadoku.get_contest_leaderboard(
            bot.session, contest_id, page=page, page_size=LOOKUP_PAGE_SIZE
        )
        entries = data.get("entries", [])
        for entry in entries:
            # Sorted descending: a zero (or negative) score means we've passed
            # everyone who actually has points.
            if entry["score"] <= 0:
                return participants
            participants.append(entry)
        if len(entries) < LOOKUP_PAGE_SIZE:
            break
    return participants


def _shame_slackers(participants: list[dict], totals: dict[str, list]) -> list[str]:
    """Names of contest participants who have points overall but none this week.

    ``participants`` is the cumulative leaderboard (highest score first);
    ``totals`` is ``_tally_scores_since`` output (keyed by user id, values
    ``[display_name, score]``). Someone is "shamed" when they appear in the
    cumulative ranking but not in the week's tally. Matching prefers the user id
    and falls back to a normalised display name, so a rename between a person's
    log and the leaderboard doesn't wrongly shame them. Returned in
    cumulative-rank order -- the higher you rank while slacking, the more
    shameful.
    """
    active_ids = set(totals.keys())
    active_names = {_normalize_name(name) for name, _ in totals.values()}
    slackers = []
    for entry in participants:
        uid = entry.get("user_id")
        if uid is not None and uid in active_ids:
            continue
        if _normalize_name(entry["user_display_name"]) in active_names:
            continue
        slackers.append(entry["user_display_name"])
    return slackers


def _format_shame_list(names: list[str]) -> str:
    """Render the shame names as a single comma-separated string.

    Shows at most ``SHAME_LIST_LIMIT`` names and collapses any overflow into an
    "…and N more" tail so the embed field stays within Discord's size limit.
    """
    shown = names[:SHAME_LIST_LIMIT]
    listed = ", ".join(shown)
    remaining = len(names) - len(shown)
    if remaining > 0:
        listed += f", …and {remaining} more"
    return listed


def _format_entry_line(entry: dict) -> str:
    """Render one ranked entry as an embed line: medal-or-#N, name, score, tie.

    Shared by /leaderboard and /weeklyleaderboard so both use identical
    formatting: a medal for the top three (else a right-aligned monospace "#N"
    so numbers line up), the display name, the one-decimal score, and an
    italic "(tie)" marker when the entry ties another.
    """
    rank = entry["rank"]
    marker = MEDALS.get(rank, f"`#{rank:>3}`")
    tie = " *(tie)*" if entry.get("is_tie") else ""
    return f"{marker} {entry['user_display_name']} — {entry['score']:.1f}{tie}"


def _card_entry(entry: dict) -> dict:
    """Map a leaderboard/ranked entry to a ``leaderboard_card`` row dict."""
    return {
        "rank": entry["rank"],
        "name": entry["user_display_name"],
        "score": entry["score"],
        "is_tie": entry.get("is_tie", False),
    }


async def _fetch_avatar(bot: commands.Bot, uid: int, cache: dict) -> Optional[bytes]:
    """Return Discord user ``uid``'s avatar bytes, or ``None`` on any failure.

    Memoised in ``cache`` so a name appearing twice (or a re-render) costs one
    download at most.
    """
    if uid in cache:
        return cache[uid]
    user = bot.get_user(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except discord.HTTPException:
            cache[uid] = None
            return None
    try:
        data = await user.display_avatar.read()
    except discord.HTTPException:
        data = None
    cache[uid] = data
    return data


async def _attach_avatars(bot: commands.Bot, guild_id: Optional[int], entries: list[dict]) -> None:
    """Fill each entry's ``avatar`` with the claimed member's Discord avatar bytes.

    Only names claimed via ``/claim`` in this guild resolve to a Discord user;
    everyone else is left without an avatar (the card draws a placeholder disc).
    Best-effort -- a lookup failure just leaves that row without a picture.
    """
    if not guild_id or not entries:
        return
    claims = config_store.get_guild_claims(guild_id)  # {discord_id_str: username}
    if not claims:
        return
    by_name = {_normalize_name(username): int(uid) for uid, username in claims.items()}
    cache: dict[int, Optional[bytes]] = {}
    for entry in entries:
        uid = by_name.get(_normalize_name(entry["name"]))
        if uid is not None:
            entry["avatar"] = await _fetch_avatar(bot, uid, cache)


async def build_yearend_card(
    bot: commands.Bot, guild_id: Optional[int]
) -> tuple[dict, Optional[leaderboard_card.LeaderboardCard]]:
    """Resolve the guild's contest and build its cumulative standings as a festive
    year-end recap card.

    Used by the year-end alert (``cogs.alerts``). Unlike the weekly/monthly
    builder, this shows the contest's *cumulative* leaderboard -- the same data
    ``/leaderboard`` displays (via ``tadoku.get_contest_leaderboard``) -- topped
    with a podium congratulation for the top three finishers. Mirrors
    ``build_period_leaderboard_card``'s contract: returns ``(contest, card)`` with
    ``card=None`` when nobody's on the leaderboard, and raises
    ``tadoku.TadokuAPIError`` if the lookup fails.
    """
    contest = await _resolve_contest(bot, guild_id)
    data = await tadoku.get_contest_leaderboard(
        bot.session, contest["id"], page=0, page_size=PAGE_SIZE
    )
    entries = data.get("entries", [])
    if not entries:
        return contest, None

    # Name the podium (however many of the top 3 exist) in the congrats line; the
    # card itself already flags them with medal badges.
    podium = ", ".join(
        entry["user_display_name"] for entry in entries[:3] if entry["rank"] in MEDALS
    )
    congrats = f"Congratulations to our top finishers — {podium}! " if podium else ""
    card_entries = [_card_entry(entry) for entry in entries]
    await _attach_avatars(bot, guild_id, card_entries)
    card = leaderboard_card.LeaderboardCard(
        title=contest["title"],
        subtitle="Final Standings",
        entries=card_entries,
        footer=f"{data.get('total_size', len(entries))} participants",
        note_body=(
            f"{congrats}Thank you all for a fantastic year of immersion. "
            "Hope to see everyone again next year!"
        ),
        accent="gold",
    )
    return contest, card


async def build_period_leaderboard_card(
    bot: commands.Bot,
    guild_id: Optional[int],
    *,
    cutoff: datetime,
    until: datetime | None = None,
    title_suffix: str,
    window_phrase: str,
) -> tuple[dict, Optional[leaderboard_card.LeaderboardCard]]:
    """Resolve the guild's contest and build a period ranking card.

    Shared by the ``/weeklyleaderboard`` / ``/monthlyleaderboard`` commands and
    the automatic wrap-up alerts (``cogs.alerts``). Ranks participants by points
    logged in the window ``[cutoff, until)`` (tallied from raw logs, since the
    API's own leaderboard is only cumulative) and, when the guild's shame setting
    is on, appends the "shame" call-out as the card's note. ``title_suffix`` names
    the window in the title; ``window_phrase`` is the prose form used in the footer
    and the shame heading. ``until`` bounds the top of the window (``None`` =
    open-ended up to now).

    Returns ``(contest, card)``. ``card`` is ``None`` when nobody logged anything
    in the window, leaving it to the caller to show an "empty" message (the
    interactive commands) or simply skip posting (the alerts). Raises
    ``tadoku.TadokuAPIError`` if resolving the contest or tallying logs fails.
    """
    contest = await _resolve_contest(bot, guild_id)
    totals = await _tally_scores_since(bot, contest["id"], cutoff, until=until)

    ranked = _rank_by_score(totals)
    if not ranked:
        return contest, None

    # When enabled for this server (on by default), add a call to shame: everyone
    # with contest points overall who logged nothing in the window.
    note_title = note_body = None
    if config_store.get_guild_shame(guild_id):
        try:
            participants = await _scored_participants(bot, contest["id"])
        except tadoku.TadokuAPIError:
            # The ranking above already succeeded; a failed shame lookup
            # shouldn't sink the whole card, so just skip the section.
            participants = []
        slackers = _shame_slackers(participants, totals)
        if slackers:
            note_title = f"Shame — logged nothing in {window_phrase}"
            note_body = _format_shame_list(slackers)

    shown = min(len(ranked), PAGE_SIZE)
    # Show the top slice; the tally already covers everyone in the window.
    card_entries = [_card_entry(entry) for entry in ranked[:PAGE_SIZE]]
    await _attach_avatars(bot, guild_id, card_entries)
    card = leaderboard_card.LeaderboardCard(
        title=contest["title"],
        subtitle=title_suffix,
        entries=card_entries,
        footer=f"Top {shown} of {len(ranked)} · points logged in {window_phrase}",
        note_title=note_title,
        note_body=note_body,
        accent="purple",
    )
    return contest, card


async def build_daily_top_card(
    bot: commands.Bot, guild_id: Optional[int], *, day_start: datetime
) -> tuple[dict, Optional[daily_card.DailyTopCard]]:
    """Resolve the guild's contest and spotlight the day's top logger.

    Tallies the logs made in the 24 hours from ``day_start`` (a UTC midnight --
    tadoku.app's own day boundary) and builds the card for whoever earned the most
    points: their Discord avatar (if they've claimed their tadoku name), their
    name, the day's total, and every title they logged with its points (most
    first). A tie for the top goes to the name that sorts first, so the pick is
    stable across retries.

    Mirrors ``build_period_leaderboard_card``'s contract: returns
    ``(contest, card)`` with ``card=None`` when nobody earned points that day, and
    raises ``tadoku.TadokuAPIError`` if resolving the contest or reading logs fails.
    """
    contest = await _resolve_contest(bot, guild_id)
    day_end = day_start + timedelta(days=1)
    people = await _tally_day(bot, contest["id"], day_start, day_end)

    ranked = sorted(
        people.values(), key=lambda person: (-person["score"], _normalize_name(person["name"]))
    )
    if not ranked or ranked[0]["score"] <= 0:
        return contest, None
    top = ranked[0]

    titles = sorted(top["titles"].values(), key=lambda row: (-row[1], row[0].casefold()))
    hero = {"name": top["name"]}
    await _attach_avatars(bot, guild_id, [hero])

    loggers = len(ranked)
    card = daily_card.DailyTopCard(
        name=top["name"],
        score=top["score"],
        titles=[(title, points) for title, points in titles],
        date_label=f"{day_start:%A}, {day_start:%B} {day_start.day}, {day_start.year}",
        footer=f"{contest['title']} · {loggers} {'person' if loggers == 1 else 'people'} logged",
        avatar=hero.get("avatar"),
    )
    return contest, card


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _language_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[Choice[str]]:
        """Autocomplete for the ``language`` filter of ``/leaderboard``.

        Only offers languages the *currently displayed contest* actually allows,
        so users can't filter by a language that isn't part of this contest.
        Contests with no explicit allow-list (all languages permitted) return no
        suggestions -- there's nothing meaningful to enumerate.
        """
        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
        except tadoku.TadokuAPIError:
            # Never let autocomplete surface an error; just suggest nothing.
            return []

        # ``allowed_languages`` may be absent or null when a contest permits any
        # language -- normalise that to an empty list.
        languages = contest.get("allowed_languages") or []
        current = current.lower()
        # Match against either the code (e.g. "jpa") or the display name.
        matches = [
            lang for lang in languages
            if current in lang.get("code", "").lower() or current in lang.get("name", "").lower()
        ]
        return [
            # Show "Name (code)" but submit the code the API expects.
            Choice(name=f"{lang['name']} ({lang['code']})", value=lang["code"])
            for lang in matches[:25]  # Discord's 25-choice cap.
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
        # Range guards against page 0 / negatives; presented to users as 1-based.
        page: app_commands.Range[int, 1, 10_000] = 1,
        language: Optional[str] = None,
        activity: Optional[Choice[int]] = None,
    ):
        """Fetch and render one page of the resolved contest's leaderboard."""
        # Fetching from tadoku.app takes a moment; defer so Discord doesn't time
        # out the interaction while we work. (Public, not ephemeral -- everyone
        # should see the leaderboard.)
        await interaction.response.defer()

        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
            data = await tadoku.get_contest_leaderboard(
                self.bot.session,
                contest["id"],
                # Users pass 1-based pages; the API is 0-based.
                page=page - 1,
                page_size=PAGE_SIZE,
                language_code=language,
                # ``activity`` is a Choice; unwrap to its id, or None if unset.
                activity_id=activity.value if activity else None,
            )
        except tadoku.TadokuAPIError:
            # Covers both resolving the contest and fetching the leaderboard.
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        entries = data.get("entries", [])
        if not entries:
            # Either the contest has no logs yet, or the user paged past the end.
            await interaction.followup.send(
                f"No leaderboard entries on page {page} for **{contest['title']}**."
            )
            return

        # Build one text line per ranked entry (shared formatting with /weeklyleaderboard).
        lines = [_format_entry_line(entry) for entry in entries]

        # Summarise any active filters for the footer, e.g. "(language: jpa, activity: Reading)".
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
        # Footer carries the metadata that doesn't belong in the ranking itself:
        # contest date range, current page, total participant count, and filters.
        embed.set_footer(
            text=(
                f"{contest['contest_start']} – {contest['contest_end']} · "
                f"Page {page} · {data.get('total_size', len(entries))} participants{filter_note}"
            )
        )
        await interaction.followup.send(embed=embed)

    async def _send_period_leaderboard(
        self,
        interaction: discord.Interaction,
        *,
        cutoff: datetime,
        until: datetime | None = None,
        title_suffix: str,
        window_phrase: str,
    ) -> None:
        """Shared body for /weeklyleaderboard and /monthlyleaderboard.

        Builds the period ranking card via ``build_period_leaderboard_card``,
        renders it to an image, and posts it -- mapping the "couldn't reach
        tadoku.app" and empty-window cases to their user-facing messages.
        """
        # Tallying logs is several network calls; defer so Discord doesn't time
        # the interaction out. Public, like /leaderboard.
        await interaction.response.defer()

        try:
            contest, card = await build_period_leaderboard_card(
                self.bot,
                interaction.guild_id,
                cutoff=cutoff,
                until=until,
                title_suffix=title_suffix,
                window_phrase=window_phrase,
            )
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        if card is None:
            # Nobody logged anything in the window (e.g. the contest ended before
            # it, or it's brand new with no logs yet).
            await interaction.followup.send(
                f"No points logged in {window_phrase} for **{contest['title']}**."
            )
            return

        png = await leaderboard_card.render(card)
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="leaderboard.png")
        )

    @app_commands.command(
        name="weeklyleaderboard",
        description="Ranking of points logged in the last 7 days of this server's contest.",
    )
    async def weeklyleaderboard(self, interaction: discord.Interaction):
        """Build a rolling 7-day ranking from the contest's raw logs.

        The contest leaderboard the API serves is cumulative, so there's no
        "last 7 days" view to fetch -- we tally it ourselves from the individual
        logs and rank users by points earned in the window.
        """
        # Window is the last 7 days ending now, in UTC (log timestamps are UTC).
        cutoff = datetime.now(timezone.utc) - timedelta(days=WEEKLY_WINDOW_DAYS)
        await self._send_period_leaderboard(
            interaction,
            cutoff=cutoff,
            title_suffix=f"last {WEEKLY_WINDOW_DAYS} days",
            window_phrase=f"the last {WEEKLY_WINDOW_DAYS} days",
        )

    @app_commands.command(
        name="monthlyleaderboard",
        description="Ranking of points logged in a calendar month of this server's contest.",
    )
    @app_commands.describe(
        month="Which month to show (defaults to the current month).",
        year="Which year to show (defaults to the current year).",
    )
    @app_commands.choices(month=MONTH_CHOICES)
    async def monthlyleaderboard(
        self,
        interaction: discord.Interaction,
        month: Optional[Choice[int]] = None,
        year: Optional[app_commands.Range[int, 2000, 2100]] = None,
    ):
        """Rank points logged in a calendar month, tallied from the raw logs.

        With no arguments this shows the current month to date. ``month`` and/or
        ``year`` select a specific (e.g. past) month; each defaults to the
        current one, so ``month:June`` alone means June of the current year. The
        window runs from the 1st of the chosen month at 00:00 UTC up to the start
        of the next month.
        """
        now = datetime.now(timezone.utc)
        target_month = month.value if month else now.month
        target_year = year if year else now.year

        # Window is [start of the chosen month, start of the next month), in UTC
        # (log timestamps are UTC). For the current month `until` is in the
        # future, so it excludes nothing -- matching the "so far this month" view.
        cutoff = datetime(target_year, target_month, 1, tzinfo=timezone.utc)
        if target_month == 12:
            until = datetime(target_year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            until = datetime(target_year, target_month + 1, 1, tzinfo=timezone.utc)
        # e.g. "June 2026" -- used for both the title and the prose phrasing.
        month_label = cutoff.strftime("%B %Y")

        await self._send_period_leaderboard(
            interaction,
            cutoff=cutoff,
            until=until,
            title_suffix=month_label,
            window_phrase=month_label,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Leaderboard(bot))
