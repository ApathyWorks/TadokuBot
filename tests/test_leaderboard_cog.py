"""Tests for the /leaderboard cog.

Monkeypatches the tadoku client functions with AsyncMocks (the HTTP contract is
covered separately in test_tadoku_client.py) so these can focus on the cog's
own logic: contest resolution (pinned vs latest-official fallback), embed
rendering (medals, ties, filter footer, 1-based paging), the empty-page and
API-error messages, and the language autocomplete. Cog commands are exercised
by calling their ``.callback(...)`` directly with a fake interaction.
"""

from datetime import datetime, timedelta, timezone
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
    monkeypatch.setattr(tadoku_client, "list_contest_logs", AsyncMock(return_value=[]))
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


# ---------------------------------------------------------------------------
# _find_leaderboard_entry (the paging scan behind /score)
# ---------------------------------------------------------------------------

def _full_page(start_rank):
    """A leaderboard page of exactly LOOKUP_PAGE_SIZE non-target entries, so the
    scan treats it as "more pages may follow" and keeps going."""
    return [
        _entry(start_rank + i, f"user{start_rank + i}", 100.0 - i)
        for i in range(leaderboard_cog.LOOKUP_PAGE_SIZE)
    ]


def _pager(pages):
    """Build a side_effect that serves ``pages`` (a {page_number: entries} map)
    and records nothing beyond what the AsyncMock already tracks."""
    def _serve(session, contest_id, *, page, page_size):
        return {"entries": pages.get(page, []), "total_size": 0}
    return _serve


async def test_find_entry_matches_on_first_page(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "ruby", 177.1), _entry(2, "ryun", 89.7)]}
    )

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "ryun")

    assert entry["user_id"] == "u2"
    # A short first page (< LOOKUP_PAGE_SIZE) means no second request.
    assert tadoku_client.get_contest_leaderboard.await_count == 1


async def test_find_entry_pages_until_match_found(fake_bot):
    # Page 0 is full (forces a second request); the target is on page 1.
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: _full_page(1), 1: [_entry(101, "target", 5.0)]}
    )

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "target")

    assert entry["rank"] == 101
    assert tadoku_client.get_contest_leaderboard.await_count == 2


async def test_find_entry_stops_paging_once_found(fake_bot):
    # Target sits on the first (full) page; the scan must not fetch page 1.
    page0 = _full_page(1)
    page0[50] = _entry(51, "target", 42.0)
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: page0, 1: _full_page(101)})

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "target")

    assert entry["rank"] == 51
    assert tadoku_client.get_contest_leaderboard.await_count == 1


async def test_find_entry_returns_none_when_absent(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "ruby", 177.1)]}
    )

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "nobody")

    assert entry is None
    # A short page ends the scan immediately -- it must not keep paging into the
    # (empty) beyond.
    assert tadoku_client.get_contest_leaderboard.await_count == 1


async def test_find_entry_match_is_case_and_whitespace_insensitive(fake_bot):
    # Leaderboard spells it "Ruby " (capital, trailing space); user types "ruby".
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "Ruby ", 177.1)]}
    )

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "ruby")

    assert entry["rank"] == 1


async def test_find_entry_respects_max_page_cap(fake_bot):
    # Every page is full and never contains the target: the scan must give up at
    # the page cap rather than looping forever.
    tadoku_client.get_contest_leaderboard.side_effect = (
        lambda session, contest_id, *, page, page_size: {
            "entries": _full_page(page * leaderboard_cog.LOOKUP_PAGE_SIZE + 1),
            "total_size": 0,
        }
    )

    entry = await leaderboard_cog._find_leaderboard_entry(fake_bot, "c1", "target")

    assert entry is None
    assert tadoku_client.get_contest_leaderboard.await_count == leaderboard_cog.MAX_LOOKUP_PAGES


# ---------------------------------------------------------------------------
# /score command
# ---------------------------------------------------------------------------

async def test_score_defers_before_scanning(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [_entry(1, "ruby", 177.1)]})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    interaction.response.defer.assert_awaited_once()


async def test_score_reports_rank_and_score_for_a_participant(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "ruby", 177.16), _entry(2, "ryun", 89.75)]}
    )
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🏆 2026 Round 4"
    assert "ruby" in embed.description
    assert "🥇" in embed.description  # rank 1 medal
    assert "177.2" in embed.description  # score, one decimal


async def test_score_shows_plain_rank_for_non_top_three(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(7, "midfielder", 12.3)]}
    )
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="midfielder")

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "#7" in embed.description


async def test_score_marks_ties(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "ruby", 100.0, is_tie=True)]}
    )
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "(tie)" in embed.description


async def test_score_uses_leaderboard_spelling_of_the_name(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "Ruby ", 100.0)]}
    )
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Ruby" in embed.description


async def test_score_reports_when_person_not_on_leaderboard(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager(
        {0: [_entry(1, "ruby", 100.0)]}
    )
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ghost")

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "ghost" in args[0]
    assert "2026 Round 4" in args[0]


async def test_score_sends_friendly_message_on_api_error(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "tadoku.app" in args[0]


async def test_score_uses_configured_contest_when_set(fake_bot):
    config_store.set_guild_contest(999, "configured-id", "Configured Contest")
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [_entry(1, "ruby", 100.0)]})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.score.callback(cog, interaction, username="ruby")

    # Scanned the configured contest, not the latest-official fallback.
    called_contest_id = tadoku_client.get_contest_leaderboard.await_args.kwargs.get("contest_id")
    if called_contest_id is None:
        called_contest_id = tadoku_client.get_contest_leaderboard.await_args.args[1]
    assert called_contest_id == "configured-id"
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "🏆 Configured Contest"


# ---------------------------------------------------------------------------
# _weekly_tally / _rank_weekly (the maths behind /weeklyleaderboard)
# ---------------------------------------------------------------------------

# A fixed cutoff used by the tally tests; timestamps below are placed relative
# to it so "inside" vs "outside" the window is explicit and deterministic.
CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    """Render a datetime as the API's Z-suffixed UTC timestamp."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _log(user_id, name, score, created_at, deleted=False):
    return {
        "user_id": user_id,
        "user_display_name": name,
        "score": score,
        "created_at": created_at,
        "deleted": deleted,
    }


def _log_pager(pages):
    """side_effect serving {page_number: [logs]} for list_contest_logs."""
    def _serve(session, contest_id, *, page, page_size):
        return pages.get(page, [])
    return _serve


async def test_weekly_tally_sums_scores_per_user_within_window(fake_bot):
    inside = _iso(CUTOFF + timedelta(hours=1))
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: [
        _log("u1", "ruby", 10, inside),
        _log("u2", "ryun", 5, inside),
        _log("u1", "ruby", 7, inside),
    ]})

    totals = await leaderboard_cog._weekly_tally(fake_bot, "c1", CUTOFF)

    assert totals["u1"] == ["ruby", 17.0]
    assert totals["u2"] == ["ryun", 5.0]


async def test_weekly_tally_stops_at_first_log_older_than_cutoff(fake_bot):
    # Newest-first order: a log before the cutoff ends the scan, so the older
    # in-list entry after it is never counted even though it's the same user.
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: [
        _log("u1", "ruby", 10, _iso(CUTOFF + timedelta(hours=1))),
        _log("u1", "ruby", 99, _iso(CUTOFF - timedelta(seconds=1))),  # older -> stop here
        _log("u2", "ryun", 50, _iso(CUTOFF + timedelta(hours=2))),    # never reached
    ]})

    totals = await leaderboard_cog._weekly_tally(fake_bot, "c1", CUTOFF)

    assert totals == {"u1": ["ruby", 10.0]}


async def test_weekly_tally_skips_deleted_logs(fake_bot):
    inside = _iso(CUTOFF + timedelta(hours=1))
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: [
        _log("u1", "ruby", 10, inside),
        _log("u1", "ruby", 100, inside, deleted=True),
    ]})

    totals = await leaderboard_cog._weekly_tally(fake_bot, "c1", CUTOFF)

    assert totals["u1"] == ["ruby", 10.0]


async def test_weekly_tally_pages_until_short_page(fake_bot):
    inside = _iso(CUTOFF + timedelta(hours=1))
    full_page = [_log(f"u{i}", f"user{i}", 1, inside) for i in range(leaderboard_cog.LOG_PAGE_SIZE)]
    tadoku_client.list_contest_logs.side_effect = _log_pager({
        0: full_page,
        1: [_log("late", "latecomer", 3, inside)],
    })

    totals = await leaderboard_cog._weekly_tally(fake_bot, "c1", CUTOFF)

    assert "late" in totals
    assert tadoku_client.list_contest_logs.await_count == 2


async def test_weekly_tally_display_name_from_newest_log(fake_bot):
    # The user renamed themselves; the newest (first) log should win.
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: [
        _log("u1", "NewName", 5, _iso(CUTOFF + timedelta(hours=2))),
        _log("u1", "OldName", 5, _iso(CUTOFF + timedelta(hours=1))),
    ]})

    totals = await leaderboard_cog._weekly_tally(fake_bot, "c1", CUTOFF)

    assert totals["u1"][0] == "NewName"


def test_rank_weekly_orders_by_score_descending():
    totals = {"u1": ["ruby", 10.0], "u2": ["ryun", 30.0], "u3": ["anja", 20.0]}

    ranked = leaderboard_cog._rank_weekly(totals)

    assert [(e["rank"], e["user_display_name"]) for e in ranked] == [
        (1, "ryun"), (2, "anja"), (3, "ruby"),
    ]


def test_rank_weekly_shares_rank_on_ties_and_skips():
    totals = {"a": ["a", 10.0], "b": ["b", 10.0], "c": ["c", 5.0]}

    ranked = leaderboard_cog._rank_weekly(totals)

    # Two tied at 10 -> both rank 1 (is_tie), next is rank 3.
    assert ranked[0]["rank"] == 1 and ranked[0]["is_tie"] is True
    assert ranked[1]["rank"] == 1 and ranked[1]["is_tie"] is True
    assert ranked[2]["rank"] == 3 and ranked[2]["is_tie"] is False


def test_rank_weekly_empty_is_empty():
    assert leaderboard_cog._rank_weekly({}) == []


# ---------------------------------------------------------------------------
# /weeklyleaderboard command
# ---------------------------------------------------------------------------

def _recent_logs(*rows):
    """Logs timestamped 'now' so they always fall inside the command's real
    (now - 7 days) window regardless of when the test runs."""
    now = _iso(datetime.now(timezone.utc))
    return [_log(uid, name, score, now) for uid, name, score in rows]


async def test_weekly_command_defers(fake_bot):
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 5))})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    interaction.response.defer.assert_awaited_once()


async def test_weekly_command_renders_ranked_embed(fake_bot):
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(
        ("u1", "ruby", 30), ("u2", "ryun", 10), ("u3", "anja", 20),
    )})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "last 7 days" in embed.title
    lines = embed.description.split("\n")
    assert lines[0] == "🥇 ruby — 30.0"   # highest weekly total
    assert lines[1] == "🥈 anja — 20.0"
    assert lines[2] == "🥉 ryun — 10.0"
    assert "3 of 3" in embed.footer.text


async def test_weekly_command_reports_empty_window(fake_bot):
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: []})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "No points logged" in args[0]
    assert "2026 Round 4" in args[0]


async def test_weekly_command_sends_friendly_message_on_api_error(fake_bot):
    tadoku_client.list_contest_logs.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "tadoku.app" in args[0]


async def test_weekly_command_uses_configured_contest(fake_bot):
    config_store.set_guild_contest(999, "configured-id", "Configured Contest")
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 5))})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    called_contest_id = tadoku_client.list_contest_logs.await_args.args[1]
    assert called_contest_id == "configured-id"
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Configured Contest" in embed.title


# ---------------------------------------------------------------------------
# shame helpers (_scored_participants / _shame_slackers / _format_shame_list)
# ---------------------------------------------------------------------------


async def test_scored_participants_collects_positive_scores_and_stops_at_zero(fake_bot):
    # Leaderboard is score-descending, so the first zero ends the scan; entries
    # after it (including later zeros) are never collected.
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [
        _entry(1, "ruby", 100.0),
        _entry(2, "ryun", 50.0),
        _entry(3, "ghost", 0.0),
        _entry(4, "also-ghost", 0.0),
    ]})

    participants = await leaderboard_cog._scored_participants(fake_bot, "c1")

    assert [p["user_display_name"] for p in participants] == ["ruby", "ryun"]


async def test_scored_participants_pages_until_short_page(fake_bot):
    tadoku_client.get_contest_leaderboard.side_effect = _pager({
        0: _full_page(1),
        1: [_entry(101, "late", 5.0)],
    })

    participants = await leaderboard_cog._scored_participants(fake_bot, "c1")

    assert len(participants) == leaderboard_cog.LOOKUP_PAGE_SIZE + 1
    assert tadoku_client.get_contest_leaderboard.await_count == 2


def test_shame_slackers_lists_participants_absent_from_weekly_totals():
    participants = [_entry(1, "ruby", 100.0), _entry(2, "ryun", 50.0), _entry(3, "anja", 10.0)]
    totals = {"u1": ["ruby", 20.0]}  # only ruby logged this week

    # ruby matched by user id and dropped; the rest kept in cumulative-rank order.
    assert leaderboard_cog._shame_slackers(participants, totals) == ["ryun", "anja"]


def test_shame_slackers_matches_by_display_name_when_ids_differ():
    # Same person logged under a different user id but the same name -> not shamed.
    participants = [_entry(1, "Ruby ", 100.0)]
    totals = {"different-id": ["ruby", 5.0]}

    assert leaderboard_cog._shame_slackers(participants, totals) == []


def test_shame_slackers_empty_when_everyone_logged():
    participants = [_entry(1, "ruby", 100.0)]
    totals = {"u1": ["ruby", 5.0]}

    assert leaderboard_cog._shame_slackers(participants, totals) == []


def test_format_shame_list_joins_names():
    assert leaderboard_cog._format_shame_list(["a", "b", "c"]) == "a, b, c"


def test_format_shame_list_caps_and_summarises_overflow():
    names = [f"n{i}" for i in range(leaderboard_cog.SHAME_LIST_LIMIT + 3)]

    out = leaderboard_cog._format_shame_list(names)

    assert out.startswith("n0, n1")
    assert "…and 3 more" in out
    # Only SHAME_LIST_LIMIT names are spelled out.
    assert f"n{leaderboard_cog.SHAME_LIST_LIMIT}" not in out


# ---------------------------------------------------------------------------
# /weeklyleaderboard shame section
# ---------------------------------------------------------------------------


async def test_weekly_command_appends_shame_field_for_slackers(fake_bot):
    # ruby logged this week; slacker has contest points but nothing this week.
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 30))})
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [
        _entry(1, "ruby", 100.0),
        _entry(2, "slacker", 40.0),
    ]})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)  # shame on by default

    await cog.weeklyleaderboard.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    shame_fields = [f for f in embed.fields if "Shame" in f.name]
    assert shame_fields
    assert "slacker" in shame_fields[0].value
    assert "ruby" not in shame_fields[0].value


async def test_weekly_command_omits_shame_field_when_disabled(fake_bot):
    config_store.set_guild_shame(999, False)
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 30))})
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [
        _entry(1, "ruby", 100.0),
        _entry(2, "slacker", 40.0),
    ]})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.fields == []
    # With shame off we don't even fetch the cumulative leaderboard.
    tadoku_client.get_contest_leaderboard.assert_not_called()


async def test_weekly_command_omits_shame_field_when_no_slackers(fake_bot):
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 30))})
    tadoku_client.get_contest_leaderboard.side_effect = _pager({0: [_entry(1, "ruby", 100.0)]})
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.fields == []


async def test_weekly_command_still_renders_when_shame_lookup_fails(fake_bot):
    tadoku_client.list_contest_logs.side_effect = _log_pager({0: _recent_logs(("u1", "ruby", 30))})
    tadoku_client.get_contest_leaderboard.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = leaderboard_cog.Leaderboard(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weeklyleaderboard.callback(cog, interaction)

    # The main ranking (from logs) still went out; only the shame section is skipped.
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "last 7 days" in embed.title
    assert embed.fields == []
