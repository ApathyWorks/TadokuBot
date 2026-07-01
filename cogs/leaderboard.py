"""Leaderboard cog: the user-facing ``/leaderboard`` command.

Resolves which contest this server should show (a pinned one, or the latest
official as a fallback), fetches a page of its ranking from tadoku.app, and
renders it as a Discord embed with medal emoji for the top three.
"""

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

        # Build one text line per ranked entry.
        lines = []
        for entry in entries:
            rank = entry["rank"]
            # Top 3 get a medal; everyone else a right-aligned "#N" in monospace
            # so the numbers line up in the embed.
            marker = MEDALS.get(rank, f"`#{rank:>3}`")
            tie = " *(tie)*" if entry.get("is_tie") else ""
            lines.append(f"{marker} {entry['user_display_name']} — {entry['score']:.1f}{tie}")

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


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Leaderboard(bot))
