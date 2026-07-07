"""Tests for the log-feed cog (the /log group + the 5-minute poller).

The tadoku client and the contest resolver are mocked; the poller's testable
core ``_poll_guild`` is driven directly with injected logs and a fixed
``last_seen`` marker, and cog callbacks are invoked directly with a fake
interaction -- no live Discord, no wall clock, no live loop.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.leaderboard as leaderboard  # noqa: F401 -- patched indirectly via tadoku_client
import cogs.log_feed as log_feed
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTEST = {"id": "c1", "title": "2026 Round 4"}
CUTOFF = "2026-07-05T20:00:00Z"


@pytest.fixture(autouse=True)
def patched(monkeypatch):
    # Empty config store -> _resolve_contest falls back to latest-official.
    monkeypatch.setattr(tadoku_client, "get_latest_official_contest", AsyncMock(return_value=CONTEST))
    monkeypatch.setattr(tadoku_client, "get_contest", AsyncMock(return_value=CONTEST))
    monkeypatch.setattr(tadoku_client, "list_contest_logs", AsyncMock(return_value=[]))
    # Per-user lifetime lookup; empty by default so claimed-logger cards show zero
    # stats unless a test provides history.
    monkeypatch.setattr(
        tadoku_client, "list_user_logs", AsyncMock(return_value={"logs": [], "total_size": 0})
    )


def _log(created_at, name="ruby", score=10, deleted=False, activity="Reading",
         amount=5, unit="Page", language="Japanese", description=None, user_id=None):
    return {
        "created_at": created_at, "user_display_name": name, "score": score, "deleted": deleted,
        "activity": activity, "amount": amount, "unit_name": unit,
        "language": language, "description": description, "user_id": user_id,
    }


def _pager(pages):
    def _serve(session, contest_id, *, page, page_size):
        return pages.get(page, [])
    return _serve


def _channel(cid=555, mention="#feed", can_send=True):
    perms = SimpleNamespace(send_messages=can_send)
    return SimpleNamespace(id=cid, mention=mention, send=AsyncMock(), permissions_for=lambda m: perms)


def _bot_with_channel(channel):
    bot = SimpleNamespace(session=AsyncMock())
    bot.get_channel = lambda cid, ch=channel: ch if (ch is not None and cid == ch.id) else None
    bot.fetch_channel = AsyncMock()
    return bot


def _user_with_avatar(url):
    return SimpleNamespace(display_avatar=SimpleNamespace(url=url))


def _http_error():
    return discord.HTTPException(SimpleNamespace(status=503, reason="err"), "boom")


# ---------------------------------------------------------------------------
# _format_log_embed
# ---------------------------------------------------------------------------

def _fields(embed):
    return {f.name: f.value for f in embed.fields}


def test_format_log_embed_includes_who_what_and_points():
    embed = log_feed._format_log_embed(
        _log(CUTOFF, name="ruby ", score=192, amount=192, unit="Page",
             activity="Reading", language="Japanese", description="奇跡を、生きている")
    )
    assert embed.author.name == "ruby"  # trailing space stripped
    assert "Reading" in embed.title
    fields = _fields(embed)
    assert fields["Amount"] == "192 Page"
    assert fields["Language"] == "Japanese"
    assert fields["Points"] == "+192"
    assert "「奇跡を、生きている」" in embed.description


def test_format_log_embed_omits_title_when_absent():
    embed = log_feed._format_log_embed(_log(CUTOFF, description=None))
    assert embed.description is None


def test_format_log_embed_omits_language_field_when_absent():
    embed = log_feed._format_log_embed(_log(CUTOFF, language=None))
    assert "Language" not in _fields(embed)


def test_format_log_embed_drops_trailing_zero_on_points():
    assert _fields(log_feed._format_log_embed(_log(CUTOFF, score=7.2000003)))["Points"] == "+7.2"
    assert _fields(log_feed._format_log_embed(_log(CUTOFF, score=3.0)))["Points"] == "+3"


def test_format_log_embed_activity_emoji():
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Reading")).title.startswith("📖")
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Listening")).title.startswith("🎧")
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Output")).title.startswith("📝")


def test_format_log_embed_sets_avatar_when_provided():
    embed = log_feed._format_log_embed(_log(CUTOFF), avatar_url="https://cdn/ruby.png")
    assert embed.author.icon_url == "https://cdn/ruby.png"


def test_format_log_embed_has_no_avatar_by_default():
    assert log_feed._format_log_embed(_log(CUTOFF)).author.icon_url is None


# ---------------------------------------------------------------------------
# profile card (two-column, with lifetime stats) + _format_count
# ---------------------------------------------------------------------------

def test_format_count_is_human_readable():
    assert log_feed._format_count(6_600_000) == "6.6M"
    assert log_feed._format_count(12_300) == "12.3k"
    assert log_feed._format_count(812) == "812"
    assert log_feed._format_count(0) == "0"


def test_format_log_embed_profile_card_has_two_columns():
    embed = log_feed._format_log_embed(
        _log(CUTOFF, name="ruby", amount=192, unit="Page", activity="Reading",
             language="Japanese", score=192),
        avatar_url="https://cdn/ruby.png",
        lifetime={"characters": 6_600_000, "pages": 1234, "minutes": 90},
    )
    assert embed.author.icon_url == "https://cdn/ruby.png"
    # Left column: lifetime stats.
    stats = next(f.value for f in embed.fields if "Immersion" in f.name)
    assert "6.6M" in stats and "1,234" in stats and "1.5h" in stats  # 90 min -> 1.5h
    # Right column: this log.
    this_log = next(f.value for f in embed.fields if "This log" in f.name)
    assert "192 Page" in this_log and "Japanese" in this_log and "+192" in this_log


def test_format_log_embed_stays_plain_without_lifetime():
    embed = log_feed._format_log_embed(_log(CUTOFF), avatar_url="https://cdn/ruby.png")
    # Avatar kept, but no lifetime column -> the usual Amount/Points fields.
    assert embed.author.icon_url == "https://cdn/ruby.png"
    assert not any("Immersion" in f.name for f in embed.fields)
    assert "Amount" in {f.name for f in embed.fields}


# ---------------------------------------------------------------------------
# _poll_guild
# ---------------------------------------------------------------------------

async def test_poll_posts_new_logs_oldest_first_skipping_deleted():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    # Newest-first, as the API returns them.
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="late"),
        _log("2026-07-05T20:30:00Z", name="gone", deleted=True),
        _log("2026-07-05T20:10:00Z", name="early"),
        _log("2026-07-05T19:00:00Z", name="old"),  # <= cutoff -> stop
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_count == 2  # "old" excluded, "gone" deleted
    first, second = [c.kwargs["embed"] for c in channel.send.await_args_list]
    assert first.author.name == "early" and second.author.name == "late"  # chronological
    # Marker advanced to the newest log.
    assert config_store.get_guild_logfeed(999)["last_seen"] == "2026-07-05T21:00:00Z"


async def test_poll_excludes_the_log_exactly_at_the_marker():
    # A log whose created_at equals last_seen was already the high-water mark and
    # must not be re-posted (guards the <= vs < boundary).
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="new"),
        _log(CUTOFF, name="marker"),  # exactly at the mark -> already seen
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_count == 1
    assert channel.send.await_args_list[0].kwargs["embed"].author.name == "new"


async def test_poll_does_nothing_when_no_new_logs():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T19:00:00Z")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    channel.send.assert_not_awaited()
    assert config_store.get_guild_logfeed(999)["last_seen"] == CUTOFF  # unchanged


async def test_poll_is_noop_when_disabled():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=False, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T21:00:00Z")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    channel.send.assert_not_awaited()


async def test_poll_is_noop_when_no_channel():
    bot = _bot_with_channel(_channel())
    config_store.set_guild_logfeed(999, enabled=True, last_seen=CUTOFF)  # channel_id absent
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise / post


async def test_poll_seeds_marker_when_missing_and_posts_nothing():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555)  # no last_seen
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    channel.send.assert_not_awaited()
    assert config_store.get_guild_logfeed(999)["last_seen"] is not None


async def test_poll_leaves_marker_and_retries_on_api_error():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    channel.send.assert_not_awaited()
    assert config_store.get_guild_logfeed(999)["last_seen"] == CUTOFF  # untouched


async def test_poll_caps_burst_with_overflow_note():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    # 25 new logs (a short page, so the scan stops after page 0).
    burst = [_log(f"2026-07-05T21:{i:02d}:00Z", name=f"u{i}") for i in range(25)]
    tadoku_client.list_contest_logs.side_effect = _pager({0: burst})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # 20 posts + 1 overflow note.
    assert channel.send.await_count == log_feed.MAX_POSTS_PER_POLL + 1
    assert "5 more" in channel.send.await_args_list[-1].args[0]


async def test_poll_pages_past_a_full_page_to_reach_the_marker():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    full = [_log(f"2026-07-05T22:{i // 60:02d}:{i % 60:02d}Z", name=f"a{i}")
            for i in range(log_feed.LOG_PAGE_SIZE)]  # all newer than cutoff, full page
    tadoku_client.list_contest_logs.side_effect = _pager({0: full, 1: [_log("2026-07-05T19:00:00Z")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert tadoku_client.list_contest_logs.await_count == 2  # had to fetch page 1


# ---------------------------------------------------------------------------
# avatar on the card for claimed loggers
# ---------------------------------------------------------------------------

async def test_poll_uses_claimed_loggers_avatar():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar("https://cdn/ruby.png") if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")  # links "ruby" -> discord user 111
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T21:00:00Z", name="Ruby ")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_args.kwargs["embed"].author.icon_url == "https://cdn/ruby.png"


async def test_poll_fetches_user_when_not_cached():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: None
    bot.fetch_user = AsyncMock(return_value=_user_with_avatar("https://cdn/ruby.png"))
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T21:00:00Z", name="ruby")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    bot.fetch_user.assert_awaited_once()
    assert channel.send.await_args.kwargs["embed"].author.icon_url == "https://cdn/ruby.png"


async def test_poll_no_avatar_for_unclaimed_logger():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)  # no get_user needed: unclaimed short-circuits
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T21:00:00Z", name="nobody")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_args.kwargs["embed"].author.icon_url is None


async def test_poll_tolerates_avatar_fetch_failure():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: None
    bot.fetch_user = AsyncMock(side_effect=_http_error())
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [_log("2026-07-05T21:00:00Z", name="ruby")]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    assert channel.send.await_args.kwargs["embed"].author.icon_url is None


async def test_poll_resolves_a_repeat_loggers_avatar_only_once():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: None
    bot.fetch_user = AsyncMock(return_value=_user_with_avatar("https://cdn/ruby.png"))
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:05:00Z", name="ruby"),
        _log("2026-07-05T21:00:00Z", name="ruby"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_count == 2  # both logs posted
    bot.fetch_user.assert_awaited_once()  # but the avatar was resolved once (cached)


# ---------------------------------------------------------------------------
# lifetime stats (_compute_lifetime) + the profile card in the poll
# ---------------------------------------------------------------------------

async def test_compute_lifetime_sums_by_unit_and_skips_deleted():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            {"unit_name": "Character", "amount": 1000, "deleted": False},
            {"unit_name": "Page", "amount": 10, "deleted": False},
            {"unit_name": "Comic page", "amount": 5, "deleted": False},   # counts as pages
            {"unit_name": "Minute", "amount": 30, "deleted": False},
            {"unit_name": "Dense minute", "amount": 60, "deleted": False},  # listening
            {"unit_name": "Sentence", "amount": 99, "deleted": False},     # ignored (not requested)
            {"unit_name": "Character", "amount": 500, "deleted": True},     # skipped
        ],
        "total_size": 7,
    }

    stats = await cog._compute_lifetime("user-1")

    assert stats == {"characters": 1000, "pages": 15, "minutes": 90}


async def test_compute_lifetime_pages_until_total_reached():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    full = [{"unit_name": "Page", "amount": 1, "deleted": False} for _ in range(log_feed.LOG_PAGE_SIZE)]
    pages = {
        0: {"logs": full, "total_size": log_feed.LOG_PAGE_SIZE + 1},
        1: {"logs": [{"unit_name": "Page", "amount": 1, "deleted": False}], "total_size": log_feed.LOG_PAGE_SIZE + 1},
    }

    async def serve(session, user_id, *, page, page_size):
        return pages[page]

    tadoku_client.list_user_logs.side_effect = serve

    stats = await cog._compute_lifetime("user-1")

    assert stats["pages"] == log_feed.LOG_PAGE_SIZE + 1
    assert tadoku_client.list_user_logs.await_count == 2


async def test_poll_builds_profile_card_for_claimed_logger():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar("https://cdn/ruby.png") if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="uuid-ruby", amount=192, unit="Page", score=192),
    ]})
    tadoku_client.list_user_logs.return_value = {
        "logs": [{"unit_name": "Character", "amount": 6_600_000, "deleted": False}],
        "total_size": 1,
    }
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    embed = channel.send.await_args.kwargs["embed"]
    assert embed.author.icon_url == "https://cdn/ruby.png"
    stats = next(f.value for f in embed.fields if "Immersion" in f.name)
    assert "6.6M" in stats
    # Summed the right user's history (log's user_id, positional arg 1).
    assert tadoku_client.list_user_logs.await_args.args[1] == "uuid-ruby"


async def test_poll_plain_card_for_unclaimed_logger_skips_lifetime():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="nobody", user_id="uuid-x"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    embed = channel.send.await_args.kwargs["embed"]
    assert embed.author.icon_url is None
    assert not any("Immersion" in f.name for f in embed.fields)
    tadoku_client.list_user_logs.assert_not_awaited()


async def test_poll_profile_card_drops_stats_when_lifetime_lookup_fails():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar("https://cdn/ruby.png") if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="uuid-ruby"),
    ]})
    tadoku_client.list_user_logs.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    embed = channel.send.await_args.kwargs["embed"]
    assert embed.author.icon_url == "https://cdn/ruby.png"  # avatar still shown
    assert not any("Immersion" in f.name for f in embed.fields)  # stats column dropped


# ---------------------------------------------------------------------------
# /log on|off|status
# ---------------------------------------------------------------------------

def _guild_interaction(guild_id=999):
    interaction = make_interaction(guild_id=guild_id)
    interaction.guild = SimpleNamespace(me=object())
    return interaction


async def test_log_on_enables_seeds_marker_and_stores_channel():
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()
    interaction.channel = _channel(cid=555)

    await log_feed.LogFeed.log_on.callback(cog, interaction, channel=None)

    settings = config_store.get_guild_logfeed(999)
    assert settings["enabled"] is True
    assert settings["channel_id"] == 555
    assert settings["last_seen"] is not None  # seeded so no backlog dump
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert "on" in args[0]


async def test_log_on_uses_explicit_channel_over_current():
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()
    interaction.channel = _channel(cid=1)
    chosen = _channel(cid=777, mention="#logs")

    await log_feed.LogFeed.log_on.callback(cog, interaction, channel=chosen)

    assert config_store.get_guild_logfeed(999)["channel_id"] == 777


async def test_log_on_refuses_channel_it_cannot_post_to():
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()
    interaction.channel = _channel(cid=555, can_send=False)

    await log_feed.LogFeed.log_on.callback(cog, interaction, channel=None)

    assert config_store.get_guild_logfeed(999)["enabled"] is False
    args, _ = interaction.response.send_message.await_args
    assert "permission" in args[0].lower()


async def test_log_off_disables():
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555)
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()

    await log_feed.LogFeed.log_off.callback(cog, interaction)

    assert config_store.get_guild_logfeed(999)["enabled"] is False
    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


async def test_log_status_reports_on():
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555)
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()

    await log_feed.LogFeed.log_status.callback(cog, interaction)

    args, _ = interaction.response.send_message.await_args
    assert "on" in args[0] and "555" in args[0]


async def test_log_status_reports_off():
    cog = log_feed.LogFeed(SimpleNamespace())
    interaction = _guild_interaction()

    await log_feed.LogFeed.log_status.callback(cog, interaction)

    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


def test_log_group_gates_at_runtime_not_via_default_permissions():
    # Access is enforced by is_admin() on each subcommand (see test_permissions.py);
    # no static default_permissions gate (it can't express "Manage Server OR a role").
    assert log_feed.LogFeed.log_group.default_permissions is None
    for cmd in (log_feed.LogFeed.log_on, log_feed.LogFeed.log_off, log_feed.LogFeed.log_status):
        assert len(cmd.checks) == 1
