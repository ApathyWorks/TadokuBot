"""Leaderboard cog: the ``/leaderboard``, ``/score``, ``/weeklyleaderboard`` and
``/monthlyleaderboard`` commands.

They all resolve which contest this server should show (a pinned one, or the
latest official as a fallback) and render results as Discord embeds with medal
emoji for the top three. ``/leaderboard`` and ``/score`` read the contest's
cumulative ranking; ``/weeklyleaderboard`` and ``/monthlyleaderboard`` instead
tally raw logs over a window (the last 7 days, or the current calendar month) to
build the rolling/period rankings the API doesn't expose directly.

When a server has the shame setting on (the default; toggled via ``/shame``),
``/weeklyleaderboard`` and ``/monthlyleaderboard`` also append a "shame"
call-out naming anyone who has points in the contest overall but logged nothing
in that command's window.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

import lib.config_store as config_store
import lib.tadoku_client as tadoku

# Emoji shown for the top three ranks; every other rank gets a plain "#N".
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# How many leaderboard rows to show per page/embed.
PAGE_SIZE = 15

# Page size used when scanning the leaderboard for a specific person. The API
# caps page_size at 100, so this is the largest (and thus fewest-requests) page.
LOOKUP_PAGE_SIZE = 100

# Safety cap on how many pages /score will scan before giving up. 50 pages of
# 100 covers 5,000 participants -- far beyond any real contest -- and bounds the
# worst case so a typo can't trigger an unbounded request loop.
MAX_LOOKUP_PAGES = 50

# The rolling window /weeklyleaderboard tallies over.
WEEKLY_WINDOW_DAYS = 7

# Most names to spell out in the /weeklyleaderboard shame list before collapsing
# the rest into an "…and N more" tail (keeps the embed field within Discord's
# 1024-char limit and readable).
SHAME_LIST_LIMIT = 15

# Page size for fetching contest logs (the API caps this at 100).
LOG_PAGE_SIZE = 100

# Safety cap on log pages fetched for the weekly tally. Logs are newest-first
# and we stop as soon as we pass the 7-day cutoff, so in practice only the
# first few pages are read; this just bounds a pathological case.
MAX_LOG_PAGES = 100

# The activity types the API supports, exposed as a fixed dropdown. The values
# are tadoku.app's activity ids (1 = reading, 2 = listening).
ACTIVITY_CHOICES = [
    Choice(name="Reading", value=1),
    Choice(name="Listening", value=2),
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


async def _tally_scores_since(bot: commands.Bot, contest_id: str, cutoff: datetime) -> dict[str, list]:
    """Sum each participant's log scores from ``cutoff`` up to now.

    Pages through the contest's logs (which arrive newest-first) and accumulates
    ``score`` per user for logs on/after ``cutoff``. Because the logs are ordered
    newest-first, the first log older than the cutoff means every remaining log
    is older too, so we stop immediately. Deleted logs are skipped.

    Returns ``{user_id: [display_name, total_score]}``; the display name is taken
    from each user's newest log in the window (the first one encountered).
    """
    totals: dict[str, list] = {}
    for page in range(MAX_LOG_PAGES):
        logs = await tadoku.list_contest_logs(bot.session, contest_id, page=page, page_size=LOG_PAGE_SIZE)
        for log in logs:
            # Newest-first ordering: once we cross the cutoff we're done entirely.
            if _parse_timestamp(log["created_at"]) < cutoff:
                return totals
            if log.get("deleted"):
                continue
            uid = log["user_id"]
            if uid not in totals:
                # First (newest) log for this user sets the display name.
                totals[uid] = [log.get("user_display_name", "Unknown"), 0.0]
            totals[uid][1] += log["score"]
        # A short/empty page is the end of the log history.
        if len(logs) < LOG_PAGE_SIZE:
            break
    return totals


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
    Bounded by ``MAX_LOOKUP_PAGES`` like the ``/score`` scan so a huge contest
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

    @app_commands.command(
        name="score",
        description="Look up a person's score in this server's current contest.",
    )
    @app_commands.describe(
        username="The person's Tadoku display name, exactly as shown on the leaderboard.",
    )
    async def score(self, interaction: discord.Interaction, username: str):
        """Report one participant's rank and score in the resolved contest.

        The person must actually be on the currently selected leaderboard; if no
        entry matches ``username`` we say so rather than inventing a zero score.
        """
        # Scanning the leaderboard is several network calls in the worst case;
        # defer so Discord doesn't time the interaction out. Public, like
        # /leaderboard -- a looked-up score is fine for everyone to see.
        await interaction.response.defer()

        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
            entry = await _find_leaderboard_entry(self.bot, contest["id"], username)
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        if entry is None:
            # The "user has to be in the currently selected leaderboard" case:
            # not found means they aren't participating in this contest.
            await interaction.followup.send(
                f"**{username}** isn't on the leaderboard for **{contest['title']}**."
            )
            return

        rank = entry["rank"]
        # Top 3 get a medal; everyone else a plain "#N".
        marker = MEDALS.get(rank, f"#{rank}")
        tie = " *(tie)*" if entry.get("is_tie") else ""

        embed = discord.Embed(
            title=f"🏆 {contest['title']}",
            # Show the leaderboard's own spelling of the name (which may differ
            # in case/spacing from what the user typed), plus rank and score.
            description=(
                f"**{entry['user_display_name']}**\n"
                f"{marker}{tie} — {entry['score']:.1f} points"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{contest['contest_start']} – {contest['contest_end']}")
        await interaction.followup.send(embed=embed)

    async def _send_period_leaderboard(
        self,
        interaction: discord.Interaction,
        *,
        cutoff: datetime,
        title_suffix: str,
        window_phrase: str,
    ) -> None:
        """Shared body for /weeklyleaderboard and /monthlyleaderboard.

        Ranks participants by points logged since ``cutoff`` -- tallied from the
        contest's raw logs, since the API's own leaderboard is only cumulative --
        and, when the guild's shame setting is on, appends a call-out of
        participants who have contest points overall but logged nothing in the
        window. The two commands differ only in the window: ``title_suffix``
        names it in the embed title, ``window_phrase`` is the prose form used in
        the footer, the empty-window message and the shame heading.
        """
        # Tallying logs is several network calls; defer so Discord doesn't time
        # the interaction out. Public, like /leaderboard.
        await interaction.response.defer()

        try:
            contest = await _resolve_contest(self.bot, interaction.guild_id)
            totals = await _tally_scores_since(self.bot, contest["id"], cutoff)
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        ranked = _rank_by_score(totals)
        if not ranked:
            # Nobody logged anything in the window (e.g. the contest ended before
            # it, or it's brand new with no logs yet).
            await interaction.followup.send(
                f"No points logged in {window_phrase} for **{contest['title']}**."
            )
            return

        # Show the top slice; the tally already covers everyone in the window.
        lines = [_format_entry_line(entry) for entry in ranked[:PAGE_SIZE]]
        embed = discord.Embed(
            title=f"🗓️ {contest['title']} — {title_suffix}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        shown = min(len(ranked), PAGE_SIZE)
        embed.set_footer(
            text=f"Top {shown} of {len(ranked)} · points logged in {window_phrase}"
        )

        # When enabled for this server (on by default), append a call to shame:
        # everyone with contest points overall who logged nothing in the window.
        if config_store.get_guild_shame(interaction.guild_id):
            try:
                participants = await _scored_participants(self.bot, contest["id"])
            except tadoku.TadokuAPIError:
                # The ranking above already succeeded; a failed shame lookup
                # shouldn't sink the whole reply, so just skip the section.
                participants = []
            slackers = _shame_slackers(participants, totals)
            if slackers:
                embed.add_field(
                    name=f"😤 Shame — logged nothing in {window_phrase}",
                    value=_format_shame_list(slackers),
                    inline=False,
                )

        await interaction.followup.send(embed=embed)

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
        description="Ranking of points logged so far this calendar month in this server's contest.",
    )
    async def monthlyleaderboard(self, interaction: discord.Interaction):
        """Rank points logged since the start of the current calendar month.

        Like /weeklyleaderboard, but the window runs from the 1st of the current
        month at 00:00 UTC up to now, tallied from the contest's raw logs.
        """
        now = datetime.now(timezone.utc)
        # Start of the current calendar month, in UTC (log timestamps are UTC).
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # e.g. "July 2026" -- used for both the title and the prose phrasing.
        month_label = now.strftime("%B %Y")
        await self._send_period_leaderboard(
            interaction,
            cutoff=cutoff,
            title_suffix=month_label,
            window_phrase=month_label,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Leaderboard(bot))
