"""Tests for the log-feed cog (the /log group + the 5-minute poller).

The tadoku client and the contest resolver are mocked; the poller's testable
core ``_poll_guild`` is driven directly with injected logs and a fixed
``last_seen`` marker, and cog callbacks are invoked directly with a fake
interaction -- no live Discord, no wall clock, no live loop.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _log(created_at, name="ruby", score=10, deleted=False, activity="Reading",
         amount=5, unit="Page", language="Japanese", description=None):
    return {
        "created_at": created_at, "user_display_name": name, "score": score, "deleted": deleted,
        "activity": activity, "amount": amount, "unit_name": unit,
        "language": language, "description": description,
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


# ---------------------------------------------------------------------------
# _format_log
# ---------------------------------------------------------------------------

def test_format_log_includes_who_what_and_points():
    line = log_feed._format_log(
        _log(CUTOFF, name="ruby ", score=192, amount=192, unit="Page",
             activity="Reading", language="Japanese", description="奇跡を、生きている")
    )
    assert "ruby" in line and "ruby " not in line  # trailing space stripped
    assert "192 Page" in line
    assert "Reading (Japanese)" in line
    assert "「奇跡を、生きている」" in line
    assert "+192 pts" in line


def test_format_log_omits_title_when_absent():
    line = log_feed._format_log(_log(CUTOFF, description=None))
    assert "「" not in line


def test_format_log_drops_trailing_zero_on_points():
    assert "+7.2 pts" in log_feed._format_log(_log(CUTOFF, score=7.2000003))
    assert "+3 pts" in log_feed._format_log(_log(CUTOFF, score=3.0))


def test_format_log_activity_emoji():
    assert log_feed._format_log(_log(CUTOFF, activity="Reading")).startswith("📖")
    assert log_feed._format_log(_log(CUTOFF, activity="Listening")).startswith("🎧")
    assert log_feed._format_log(_log(CUTOFF, activity="Output")).startswith("📝")


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
    first, second = [c.args[0] for c in channel.send.await_args_list]
    assert "early" in first and "late" in second  # chronological order
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
    assert "marker" not in channel.send.await_args_list[0].args[0]


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


def test_log_group_requires_manage_guild():
    perms = log_feed.LogFeed.log_group.default_permissions
    assert perms is not None and perms.manage_guild is True
