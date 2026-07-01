"""Tests for the /leaderboard cog.

Monkeypatches the tadoku client functions with AsyncMocks (the HTTP contract is
covered separately in test_tadoku_client.py) so these can focus on the cog's
own logic: contest resolution (pinned vs latest-official fallback), embed
rendering (medals, ties, filter footer, 1-based paging), the empty-page and
API-error messages, and the language autocomplete. Cog commands are exercised
by calling their ``.callback(...)`` directly with a fake interaction.
"""

from unittest.mock import AsyncMock

import pytest
from discord.app_commands import Choice

import cogs.leaderboard as leaderboard_cog
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

LATEST_OFFICIAL = {
    "id": "latest-id",
    "title": "2026 Round 4",
    "contest_start": "2026-07-01",
    "contest_end": "2026-07-31",
}

CONFIGURED_CONTEST = {
    "id": "configured-id",
    "title": "Configured Contest",
    "contest_start": "2026-01-01",
    "contest_end": "2026-01-31",
}


@pytest.fixture(autouse=True)
def patched_tadoku(monkeypatch):
    monkeypatch.setattr(tadoku_client, "get_latest_official_contest", AsyncMock(return_value=LATEST_OFFICIAL))
    monkeypatch.setattr(tadoku_client, "get_contest", AsyncMock(return_value=CONFIGURED_CONTEST))
    monkeypatch.setattr(
        tadoku_client,
        "get_contest_leaderboard",
        AsyncMock(return_value={"entries": [], "total_size": 0}),
    )
    return tadoku_client


# ---------------------------------------------------------------------------
# _resolve_contest
# ---------------------------------------------------------------------------

async def test_resolve_contest_falls_back_to_latest_official_when_unconfigured(fake_bot):
    contest = await leaderboard_cog._resolve_contest(fake_bot, guild_id=999)

    assert contest == LATEST_OFFICIAL
    tadoku_client.get_latest_official_contest.assert_awaited_once_with(fake_bot.session)
    tadoku_client.get_contest.assert_not_called()


async def test_resolve_contest_falls_back_to_latest_official_when_no_guild(fake_bot):
    contest = await leaderboard_cog._resolve_contest(fake_bot, guild_id=None)

    assert contest == LATEST_OFFICIAL
    tadoku_client.get_latest_official_contest.assert_awaited_once()


async def test_resolve_contest_uses_configured_contest(fake_bot):
    config_store.set_guild_contest(999, "configured-id", "Configured Contest")

    contest = await leaderboard_cog._resolve_contest(fake_bot, guild_id=999)

    assert contest == CONFIGURED_CONTEST
    tadoku_client.get_contest.assert_awaited_once_with(fake_bot.session, "configured-id")
    tadoku_client.get_latest_official_contest.assert_not_called()


# ---------------------------------------------------------------------------
# /leaderboard command
# ---------------------------------------------------------------------------

def _entry(rank, name, score, is_tie=False):
    return {"rank": rank, "user_id": f"u{rank}", "user_display_name": name, "score": score, "is_tie": is_tie}


async def test_leaderboard_defers_before_calling_the_api(fake_bot):
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    interaction.response.defer.assert_awaited_once()


async def test_leaderboard_builds_embed_with_medals_and_plain_ranks(fake_bot):
    tadoku_client.get_contest_leaderboard.return_value = {
        "entries": [
            _entry(1, "ruby", 177.16249),
            _entry(2, "tampopoi", 89.7575),
            _entry(3, "ryun", 75.0),
            _entry(4, "eebeejay", 57.000004),
        ],
        "total_size": 27,
    }
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🏆 2026 Round 4"
    lines = embed.description.split("\n")
    assert lines[0] == "🥇 ruby — 177.2"
    assert lines[1] == "🥈 tampopoi — 89.8"
    assert lines[2] == "🥉 ryun — 75.0"
    assert lines[3] == "`#  4` eebeejay — 57.0"
    assert "27 participants" in embed.footer.text
    assert "Page 1" in embed.footer.text
    assert "2026-07-01 – 2026-07-31" in embed.footer.text


async def test_leaderboard_marks_ties(fake_bot):
    tadoku_client.get_contest_leaderboard.return_value = {
        "entries": [_entry(1, "ruby", 100.0, is_tie=True)],
        "total_size": 1,
    }
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "(tie)" in embed.description


async def test_leaderboard_appends_filter_note_when_filters_used(fake_bot):
    tadoku_client.get_contest_leaderboard.return_value = {
        "entries": [_entry(1, "ruby", 100.0)],
        "total_size": 1,
    }
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(
        cog, interaction, page=1, language="jpa", activity=Choice(name="Reading", value=1)
    )

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "language: jpa" in embed.footer.text
    assert "activity: Reading" in embed.footer.text


async def test_leaderboard_omits_filter_note_when_no_filters(fake_bot):
    tadoku_client.get_contest_leaderboard.return_value = {
        "entries": [_entry(1, "ruby", 100.0)],
        "total_size": 1,
    }
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "(" not in embed.footer.text.split("participants")[-1]


async def test_leaderboard_page_is_zero_indexed_for_the_api_call(fake_bot):
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=3, language=None, activity=None)

    tadoku_client.get_contest_leaderboard.assert_awaited_once_with(
        fake_bot.session, LATEST_OFFICIAL["id"], page=2, page_size=leaderboard_cog.PAGE_SIZE,
        language_code=None, activity_id=None,
    )


async def test_leaderboard_passes_activity_id_from_choice_value(fake_bot):
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(
        cog, interaction, page=1, language=None, activity=Choice(name="Listening", value=2)
    )

    _, kwargs = tadoku_client.get_contest_leaderboard.await_args
    assert kwargs["activity_id"] == 2


async def test_leaderboard_sends_empty_message_when_no_entries(fake_bot):
    tadoku_client.get_contest_leaderboard.return_value = {"entries": [], "total_size": 0}
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=2, language=None, activity=None)

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "page 2" in args[0].lower()
    assert "2026 Round 4" in args[0]


async def test_leaderboard_sends_friendly_message_on_api_error(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "tadoku.app" in args[0]


async def test_leaderboard_sends_friendly_message_when_resolve_contest_fails(fake_bot):
    tadoku_client.get_latest_official_contest.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.leaderboard.callback(cog, interaction, page=1, language=None, activity=None)

    tadoku_client.get_contest_leaderboard.assert_not_called()
    args, kwargs = interaction.followup.send.await_args
    assert "tadoku.app" in args[0]


# ---------------------------------------------------------------------------
# language autocomplete
# ---------------------------------------------------------------------------

async def test_language_autocomplete_filters_by_code_or_name(fake_bot):
    tadoku_client.get_latest_official_contest.return_value = {
        **LATEST_OFFICIAL,
        "allowed_languages": [
            {"code": "jpa", "name": "Japanese"},
            {"code": "zho", "name": "Chinese"},
        ],
    }
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._language_autocomplete(interaction, "jap")

    assert [c.value for c in choices] == ["jpa"]


async def test_language_autocomplete_empty_when_contest_allows_all_languages(fake_bot):
    tadoku_client.get_latest_official_contest.return_value = {**LATEST_OFFICIAL, "allowed_languages": None}
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._language_autocomplete(interaction, "")

    assert choices == []


async def test_language_autocomplete_returns_empty_list_on_api_error(fake_bot):
    tadoku_client.get_latest_official_contest.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    choices = await cog._language_autocomplete(interaction, "jap")

    assert choices == []
