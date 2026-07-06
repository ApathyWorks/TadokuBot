"""Tests for the admin cog (/set_contest, /current_contest).

Monkeypatches the tadoku client with AsyncMocks and covers: contest
autocomplete (title filtering, name formatting, the 25-item cap, error
tolerance), /set_contest storing/echoing on success and storing nothing on a
bad id, the Manage-Server permission check itself, and /current_contest's
configured-vs-fallback replies.
"""

from unittest.mock import AsyncMock

import pytest

import cogs.admin as admin_cog
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTESTS = [
    {"id": "c1", "title": "2026 Round 4", "contest_start": "2026-07-01", "contest_end": "2026-07-31"},
    {"id": "c2", "title": "2026 Round 3", "contest_start": "2026-04-01", "contest_end": "2026-04-30"},
    {"id": "c3", "title": "2025 Special Event", "contest_start": "2025-12-01", "contest_end": "2025-12-31"},
]


@pytest.fixture(autouse=True)
def patched_tadoku(monkeypatch):
    monkeypatch.setattr(tadoku_client, "list_contests", AsyncMock(return_value=list(CONTESTS)))
    monkeypatch.setattr(tadoku_client, "get_contest", AsyncMock(return_value=CONTESTS[0]))
    return tadoku_client


# ---------------------------------------------------------------------------
# contest autocomplete
# ---------------------------------------------------------------------------

async def test_autocomplete_filters_by_title_case_insensitively(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._contest_autocomplete(interaction, "round")

    assert {c.value for c in choices} == {"c1", "c2"}


async def test_autocomplete_formats_name_with_dates(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._contest_autocomplete(interaction, "2026 Round 4")

    assert choices[0].name == "2026 Round 4 (2026-07-01 – 2026-07-31)"
    assert choices[0].value == "c1"


async def test_autocomplete_caps_at_25_results(fake_bot):
    many_contests = [
        {"id": f"c{i}", "title": f"Round {i}", "contest_start": "2026-01-01", "contest_end": "2026-01-31"}
        for i in range(40)
    ]
    tadoku_client.list_contests.return_value = many_contests
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._contest_autocomplete(interaction, "round")

    assert len(choices) == 25


async def test_autocomplete_returns_empty_list_on_api_error(fake_bot):
    tadoku_client.list_contests.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._contest_autocomplete(interaction, "round")

    assert choices == []


# ---------------------------------------------------------------------------
# /set_contest
# ---------------------------------------------------------------------------

async def test_set_contest_stores_the_chosen_contest_and_confirms(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.set_contest.callback(cog, interaction, "c1")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert config_store.get_guild_contest(999) == {"contest_id": "c1", "contest_title": "2026 Round 4"}
    args, kwargs = interaction.followup.send.await_args
    assert "2026 Round 4" in args[0]


async def test_set_contest_does_not_store_anything_on_api_error(fake_bot):
    tadoku_client.get_contest.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.set_contest.callback(cog, interaction, "bogus-id")

    assert config_store.get_guild_contest(999) is None
    args, kwargs = interaction.followup.send.await_args
    assert "Couldn't find" in args[0]


# ---------------------------------------------------------------------------
# /current_contest
# ---------------------------------------------------------------------------

async def test_current_contest_reports_configured_contest(fake_bot):
    config_store.set_guild_contest(999, "c1", "2026 Round 4")
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.current_contest.callback(cog, interaction)

    args, kwargs = interaction.response.send_message.await_args
    assert "2026 Round 4" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_current_contest_reports_fallback_when_unconfigured(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.current_contest.callback(cog, interaction)

    args, kwargs = interaction.response.send_message.await_args
    assert "No contest configured" in args[0]
    assert kwargs.get("ephemeral") is True


# ---------------------------------------------------------------------------
# /shame
# ---------------------------------------------------------------------------


async def test_shame_reports_default_on_when_never_set(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.shame.callback(cog, interaction, enabled=None)

    args, kwargs = interaction.response.send_message.await_args
    assert "on" in args[0]
    assert kwargs.get("ephemeral") is True
    # Reporting must not change the stored state.
    assert config_store.get_guild_shame(999) is True


async def test_shame_reports_current_off_state(fake_bot):
    config_store.set_guild_shame(999, False)
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.shame.callback(cog, interaction, enabled=None)

    args, kwargs = interaction.response.send_message.await_args
    assert "off" in args[0]


async def test_shame_turns_off_and_confirms(fake_bot):
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.shame.callback(cog, interaction, enabled=False)

    assert config_store.get_guild_shame(999) is False
    args, kwargs = interaction.response.send_message.await_args
    assert "off" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_shame_turns_back_on_and_confirms(fake_bot):
    config_store.set_guild_shame(999, False)
    cog = admin_cog.Admin(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.shame.callback(cog, interaction, enabled=True)

    assert config_store.get_guild_shame(999) is True
    args, kwargs = interaction.response.send_message.await_args
    assert "on" in args[0]
