"""Resolve linked Tadoku participants who have left a Discord server."""

import logging

import discord

from lib import config_store

_log = logging.getLogger(__name__)


async def departed_names(bot, guild_id: int | None) -> set[str]:
    """Return normalized names only for confirmed departures.

    Fetch individual members live: the bot does not enable the privileged
    members intent, so its member cache cannot reliably track joins/leaves.
    Claims are preserved, and no absence is cached across leaderboard builds,
    allowing rejoins (and departures while the bot was offline) to take effect.
    Unlinked participants and failed/unknown lookups are left visible.
    """
    if guild_id is None:
        return set()
    claims = config_store.get_guild_claims(guild_id)
    if not claims:
        return set()
    guild = bot.get_guild(guild_id)
    if guild is None:
        return set()
    departed = set()
    for user_id, name in claims.items():
        try:
            await guild.fetch_member(int(user_id))
        except discord.NotFound as exc:
            if exc.code == 10007:  # Unknown Member, not Unknown Guild/access failure.
                departed.add(name.strip().casefold())
            else:
                _log.warning("Membership lookup failed for guild %s user %s: %s", guild_id, user_id, exc)
        except discord.HTTPException as exc:
            _log.warning("Membership lookup failed for guild %s user %s: %s", guild_id, user_id, exc)
    return departed
