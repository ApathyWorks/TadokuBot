"""Claims cog: link Discord users to their tadoku.app usernames.

Once a member is "claimed" to a tadoku display name, the bot knows which Discord
user is which contest participant. The mapping is per-server and two-way unique:
each member may claim at most one username, and each username may be claimed by
at most one member.

Commands:
  * ``/claim username`` -- a member links themselves to a contest participant.
  * ``/unclaim``        -- a member drops their own claim.
  * ``/unclaimedlist``  -- list contest participants nobody has claimed yet.
  * ``/autoclaim``      -- (admin) bulk-link every participant to the Discord
    member of the same name.

Matching (and uniqueness) is case/whitespace-insensitive, reusing the
leaderboard cog's ``_normalize_name`` so it agrees with ``/score``. Contest data
is fetched live from tadoku.app via the leaderboard cog's helpers; only the
claim map is persisted (in ``lib.config_store``).
"""

import discord
from discord import app_commands
from discord.ext import commands

import cogs.leaderboard as leaderboard
import lib.config_store as config_store
import lib.tadoku_client as tadoku
from lib.permissions import is_admin

# Fold names the same way /score does, so claims match the leaderboard.
_normalize = leaderboard._normalize_name

# Cap how many names /unclaimedlist spells out before an "…and N more" tail, so
# the embed stays within Discord's field limit.
UNCLAIMED_LIST_LIMIT = 40

# Upper bound on members Discord returns per name lookup in /autoclaim. The
# gateway caps this at 100; we only keep exact-name matches anyway.
MEMBER_QUERY_LIMIT = 100


def _member_names(member: discord.Member) -> set[str]:
    """The normalized names a member could be matched by: username, nick, global."""
    names = {_normalize(member.name), _normalize(member.display_name)}
    global_name = getattr(member, "global_name", None)
    if global_name:
        names.add(_normalize(global_name))
    return names


def _find_claimer(claims: dict[str, str], username: str) -> str | None:
    """Return the Discord id (str) already claiming ``username``, or ``None``.

    Matches case/whitespace-insensitively so "Ruby " and "ruby" collide -- that's
    what enforces "no tadoku username used twice".
    """
    target = _normalize(username)
    for uid, claimed in claims.items():
        if _normalize(claimed) == target:
            return uid
    return None


class Claims(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _resolve_member(self, guild: discord.Guild, name: str) -> discord.Member | None:
        """Return the single guild member whose name matches ``name``, else None.

        Uses ``query_members`` (a gateway name search that works without the
        privileged members intent) and keeps only exact normalized matches on
        username / nick / global name. Returns ``None`` if nobody matches or if
        the name is ambiguous (more than one member) -- better to skip than to
        pair the wrong person. A blank name or a lookup failure also yields None.
        """
        query = name.strip()
        if not query:
            return None
        try:
            candidates = await guild.query_members(query=query, limit=MEMBER_QUERY_LIMIT)
        except (discord.HTTPException, discord.ClientException):
            return None
        target = _normalize(name)
        matches = [m for m in candidates if target in _member_names(m)]
        return matches[0] if len(matches) == 1 else None

    @app_commands.command(
        name="claim",
        description="Link your Discord account to your tadoku.app username.",
    )
    @app_commands.describe(username="Your Tadoku display name, exactly as shown on the leaderboard.")
    @app_commands.guild_only()
    async def claim(self, interaction: discord.Interaction, username: str):
        """Link the caller to a contest participant by tadoku display name.

        Rejects the claim if the caller already has one, if the username is
        already claimed by someone else, or if the name isn't on the current
        contest's leaderboard (guards against typos). Stores the leaderboard's
        canonical spelling of the name.
        """
        # Resolving the contest/leaderboard is a network call; defer ephemerally.
        await interaction.response.defer(ephemeral=True)

        claims = config_store.get_guild_claims(interaction.guild_id)
        mine = claims.get(str(interaction.user.id))
        if mine is not None:
            await interaction.followup.send(
                f"You've already claimed **{mine}**. Use `/unclaim` first if you want to change it."
            )
            return

        try:
            contest = await leaderboard._resolve_contest(self.bot, interaction.guild_id)
            entry = await leaderboard._find_leaderboard_entry(self.bot, contest["id"], username)
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        if entry is None:
            await interaction.followup.send(
                f"**{username}** isn't on the leaderboard for **{contest['title']}**. Check the spelling."
            )
            return

        # Use the leaderboard's own spelling for storage and display.
        canonical = entry["user_display_name"]
        taken_by = _find_claimer(claims, canonical)
        if taken_by is not None:
            await interaction.followup.send(
                f"**{canonical}** is already claimed by <@{taken_by}>."
            )
            return

        config_store.set_claim(interaction.guild_id, interaction.user.id, canonical)
        await interaction.followup.send(f"✅ You're now linked to **{canonical}**.")

    @app_commands.command(
        name="unclaim",
        description="Remove the tadoku.app username you claimed.",
    )
    @app_commands.guild_only()
    async def unclaim(self, interaction: discord.Interaction):
        """Drop the caller's claim, freeing the username for anyone else."""
        removed = config_store.remove_claim(interaction.guild_id, interaction.user.id)
        if removed is None:
            await interaction.response.send_message(
                "You haven't claimed a username. Use `/claim` to link one.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Unclaimed **{removed}**. It's free for anyone to claim now.", ephemeral=True
        )

    @app_commands.command(
        name="unclaimedlist",
        description="List contest participants nobody has claimed yet.",
    )
    @app_commands.guild_only()
    async def unclaimedlist(self, interaction: discord.Interaction):
        """Show the current contest's participants with no Discord claim."""
        await interaction.response.defer(ephemeral=True)

        try:
            contest = await leaderboard._resolve_contest(self.bot, interaction.guild_id)
            participants = await leaderboard._scored_participants(self.bot, contest["id"])
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        claimed_names = {_normalize(u) for u in config_store.get_guild_claims(interaction.guild_id).values()}
        unclaimed = [
            p["user_display_name"]
            for p in participants
            if _normalize(p["user_display_name"]) not in claimed_names
        ]

        if not unclaimed:
            await interaction.followup.send(
                f"Everyone on **{contest['title']}** has been claimed. 🎉"
            )
            return

        shown = unclaimed[:UNCLAIMED_LIST_LIMIT]
        listed = ", ".join(shown)
        remaining = len(unclaimed) - len(shown)
        if remaining > 0:
            listed += f", …and {remaining} more"

        embed = discord.Embed(
            title=f"Unclaimed — {contest['title']}",
            description=listed,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(unclaimed)} unclaimed of {len(participants)} participants")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="autoclaim",
        description="Auto-link contest participants to Discord members of the same name.",
    )
    @app_commands.guild_only()
    @is_admin()
    async def autoclaim(self, interaction: discord.Interaction):
        """Bulk-claim: pair each unclaimed participant with a same-named member.

        Skips participants whose username is already claimed, members who already
        have a claim, and any name that matches zero or multiple members (to avoid
        mis-pairing). Existing claims are never overwritten.
        """
        # Scanning the leaderboard plus a member lookup per name is many calls;
        # defer ephemerally and summarise at the end.
        await interaction.response.defer(ephemeral=True)

        try:
            contest = await leaderboard._resolve_contest(self.bot, interaction.guild_id)
            participants = await leaderboard._scored_participants(self.bot, contest["id"])
        except tadoku.TadokuAPIError:
            await interaction.followup.send(
                "❌ Couldn't reach tadoku.app right now. Try again in a moment."
            )
            return

        claims = config_store.get_guild_claims(interaction.guild_id)
        claimed_users = set(claims.keys())  # Discord ids (str) already linked
        claimed_names = {_normalize(u) for u in claims.values()}

        paired = 0
        for entry in participants:
            name = entry["user_display_name"]
            if _normalize(name) in claimed_names:
                continue  # this username is already claimed
            member = await self._resolve_member(interaction.guild, name)
            if member is None:
                continue  # no unique same-named member
            if str(member.id) in claimed_users:
                continue  # that member already claimed someone else
            config_store.set_claim(interaction.guild_id, member.id, name)
            claimed_users.add(str(member.id))
            claimed_names.add(_normalize(name))
            paired += 1

        await interaction.followup.send(
            f"✅ Auto-linked **{paired}** participant(s) to Discord members by name for "
            f"**{contest['title']}**. Anyone left over can `/claim` themselves — see `/unclaimedlist`."
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Claims(bot))
