"""Live membership checks retain claims and recover automatically on rejoin."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from lib import config_store, membership


def _not_found(code=10007):
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                            {"code": code, "message": "Not found"})


async def test_departure_and_rejoin_are_checked_live_and_claim_is_preserved(fake_bot):
    config_store.set_claim(999, 111, " Ruby ")
    guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=[_not_found(), object()]))
    fake_bot.get_guild = lambda gid: guild
    assert await membership.departed_names(fake_bot, 999) == {"ruby"}
    assert await membership.departed_names(fake_bot, 999) == set()
    assert config_store.get_guild_claims(999) == {"111": " Ruby "}
    assert guild.fetch_member.await_count == 2


@pytest.mark.parametrize("error", [
    _not_found(10004),
    discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No access"),
    discord.HTTPException(SimpleNamespace(status=500, reason="Error"), "Unavailable"),
])
async def test_lookup_failure_does_not_count_as_departure(fake_bot, error):
    config_store.set_claim(999, 111, "ruby")
    guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=error))
    fake_bot.get_guild = lambda gid: guild
    assert await membership.departed_names(fake_bot, 999) == set()


async def test_claims_and_membership_are_guild_specific(fake_bot):
    config_store.set_claim(999, 111, "ruby")
    config_store.set_claim(888, 111, "ruby")
    guilds = {
        999: SimpleNamespace(fetch_member=AsyncMock(side_effect=_not_found())),
        888: SimpleNamespace(fetch_member=AsyncMock(return_value=object())),
    }
    fake_bot.get_guild = guilds.get
    assert await membership.departed_names(fake_bot, 999) == {"ruby"}
    assert await membership.departed_names(fake_bot, 888) == set()
    assert await membership.departed_names(fake_bot, None) == set()
    assert await membership.departed_names(fake_bot, 777) == set()
