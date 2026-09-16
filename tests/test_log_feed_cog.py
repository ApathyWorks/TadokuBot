"""Tests for the log-feed cog (the /log group + the 1-minute poller).

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
import lib.profile_card as profile_card
import lib.rank_card as rank_card
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTEST = {"id": "c1", "title": "2026 Round 4",
           "contest_start": "2026-07-01", "contest_end": "2026-07-31"}
CUTOFF = "2026-07-05T20:00:00Z"


@pytest.fixture(autouse=True)
def patched(monkeypatch):
    # Empty config store -> _resolve_contest falls back to latest-official.
    monkeypatch.setattr(tadoku_client, "get_latest_official_contest", AsyncMock(return_value=CONTEST))
    monkeypatch.setattr(tadoku_client, "get_contest", AsyncMock(return_value=CONTEST))
    monkeypatch.setattr(tadoku_client, "list_contest_logs", AsyncMock(return_value=[]))
    # Per-user contest-stats lookup; empty by default so claimed-logger cards show
    # zero stats unless a test provides history.
    monkeypatch.setattr(
        tadoku_client, "list_user_logs", AsyncMock(return_value={"logs": [], "total_size": 0})
    )
    # Contest leaderboard drives the card subtitle (place + score); empty by
    # default so a claimed logger who isn't ranked just gets a blank subtitle.
    monkeypatch.setattr(
        tadoku_client, "get_contest_leaderboard",
        AsyncMock(return_value={"entries": [], "total_size": 0}),
    )
    # Stub the (Pillow) image renderer so poll tests don't render real PNGs; the
    # renderer itself is covered in test_profile_card.py.
    monkeypatch.setattr(profile_card, "render_card", AsyncMock(return_value=b"PNGDATA"))
    # Same for the rank-change card; poll tests assert on the card model passed in.
    monkeypatch.setattr(rank_card, "render", AsyncMock(return_value=b"RANKPNG"))


def _log(created_at, name="ruby", score=10, deleted=False, activity="Reading",
         amount=5, unit="Page", language="Japanese", description=None, user_id=None, tags=None,
         duration_seconds=None):
    return {
        "created_at": created_at, "user_display_name": name, "score": score, "deleted": deleted,
        "activity": activity, "amount": amount, "unit_name": unit,
        "language": language, "description": description, "user_id": user_id, "tags": tags,
        "duration_seconds": duration_seconds,
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


def _user_with_avatar(data=b"AVATARBYTES"):
    """A fake user whose avatar asset reads to ``data`` bytes."""
    return SimpleNamespace(display_avatar=SimpleNamespace(read=AsyncMock(return_value=data)))


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


def test_format_log_embed_displays_time_tracked_duration():
    embed = log_feed._format_log_embed(
        _log(CUTOFF, activity="Listening", amount=0, unit="", duration_seconds=5_490)
    )

    assert _fields(embed)["Amount"] == "91.5 minutes"


def test_format_tracking_uses_singular_minute_for_one_minute():
    value = log_feed._format_tracking(
        _log(CUTOFF, activity="Listening", amount=0, unit="", duration_seconds=60)
    )

    assert value == "1 minute"


def test_format_tracking_listening_keeps_larger_duration_only():
    value = log_feed._format_tracking(
        _log(CUTOFF, activity="Listening", amount=30, unit="Minute", duration_seconds=3_600)
    )

    assert value == "60 minutes"


def test_format_tracking_listening_keeps_larger_amount_only():
    value = log_feed._format_tracking(
        _log(CUTOFF, activity="Listening", amount=90, unit="Minute", duration_seconds=3_600)
    )

    assert value == "90 Minute"


def test_format_tracking_listening_tie_prefers_duration():
    value = log_feed._format_tracking(
        _log(CUTOFF, activity="Listening", amount=60, unit="Minute", duration_seconds=3_600)
    )

    assert value == "60 minutes"


def test_format_tracking_reading_preserves_amount_and_duration():
    value = log_feed._format_tracking(
        _log(CUTOFF, activity="Reading", amount=40, unit="Page", duration_seconds=5_400)
    )

    assert value == "40 Page · 90 minutes"


def test_format_log_embed_activity_emoji():
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Reading")).title.startswith("📖")
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Listening")).title.startswith("🎧")
    assert log_feed._format_log_embed(_log(CUTOFF, activity="Output")).title.startswith("📝")


def test_format_log_embed_sets_avatar_when_provided():
    embed = log_feed._format_log_embed(_log(CUTOFF), avatar_url="https://cdn/ruby.png")
    assert embed.author.icon_url == "https://cdn/ruby.png"


def test_format_log_embed_has_no_avatar_by_default():
    assert log_feed._format_log_embed(_log(CUTOFF)).author.icon_url is None


def test_this_log_line_has_activity_amount_points_no_language():
    line = log_feed._this_log_line(_log(CUTOFF, activity="Reading", amount=192, unit="Page",
                                        language="Japanese", score=192))
    assert "Reading" in line and "192 Page" in line and "+192 pts" in line
    assert "Japanese" not in line  # language deliberately dropped from the card


def test_this_log_line_shows_tags_before_the_activity():
    line = log_feed._this_log_line(_log(CUTOFF, activity="Reading", amount=192, unit="Page",
                                        score=192, tags=["fiction", "game"]))
    assert line.startswith("#fiction #game")
    assert line.index("#fiction") < line.index("Reading")


def test_this_log_line_dedupes_tags_case_insensitively_and_keeps_order():
    line = log_feed._this_log_line(_log(CUTOFF, activity="Reading", tags=["Manga", "manga", "book"]))
    assert line.startswith("#Manga #book  ·  Reading")


def test_this_log_line_without_tags_is_unchanged():
    line = log_feed._this_log_line(_log(CUTOFF, activity="Listening", amount=30, unit="Minute",
                                        score=30, tags=None))
    assert line.startswith("Listening")
    assert "#" not in line


def test_this_log_line_displays_duration_without_empty_amount():
    line = log_feed._this_log_line(
        _log(CUTOFF, activity="Listening", amount=0, unit="", duration_seconds=1_800)
    )

    assert line == "Listening  ·  30 minutes  ·  +10 pts"


# ---------------------------------------------------------------------------
# _youtube_urls
# ---------------------------------------------------------------------------

def test_youtube_urls_extracts_single_link():
    log = _log(CUTOFF, tags=["youtube"], description="面白い動画 https://youtu.be/abc123")
    assert log_feed._youtube_urls(log) == ["https://youtu.be/abc123"]


def test_youtube_urls_extracts_all_links_in_order():
    log = _log(CUTOFF, tags=["youtube"],
               description="part 1 https://youtu.be/one and https://youtu.be/two")
    assert log_feed._youtube_urls(log) == ["https://youtu.be/one", "https://youtu.be/two"]


def test_youtube_urls_empty_without_the_youtube_tag():
    # A URL in the description but no youtube tag -> we don't post the link.
    assert log_feed._youtube_urls(_log(CUTOFF, tags=["video"], description="https://youtu.be/x")) == []


def test_youtube_urls_empty_when_description_has_no_url():
    assert log_feed._youtube_urls(_log(CUTOFF, tags=["youtube"], description="just a title")) == []


def test_youtube_urls_empty_without_tags():
    assert log_feed._youtube_urls(_log(CUTOFF, description="https://youtu.be/x")) == []


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
# the rendered profile card for claimed loggers
# ---------------------------------------------------------------------------

def _sent(channel):
    """The keyword args of the most recent channel.send call."""
    return channel.send.await_args.kwargs


async def test_poll_renders_image_card_for_claimed_logger():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar(b"AV") if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")  # links "ruby" -> discord user 111
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="Ruby ", user_id="uuid-ruby", amount=192, unit="Page", score=192),
    ]})
    tadoku_client.list_user_logs.return_value = {
        "logs": [{"unit_name": "Character", "amount": 6_600_000, "deleted": False,
                  "created_at": "2026-07-05T20:00:00Z"}],
        "total_size": 1,
    }
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # Posted as an attached image, not an embed.
    sent = _sent(channel)
    assert isinstance(sent["file"], discord.File)
    assert sent.get("embed") is None
    # Renderer got the logger's name, avatar bytes, contest stats and this-log line.
    kwargs = profile_card.render_card.await_args.kwargs
    assert kwargs["display_name"] == "Ruby"
    assert kwargs["avatar_bytes"] == b"AV"
    assert kwargs["characters"] == 6_600_000
    assert "Reading" in kwargs["this_log"] and "192 Page" in kwargs["this_log"]
    # And it summed the right user's history.
    assert tadoku_client.list_user_logs.await_args.args[1] == "uuid-ruby"


async def test_poll_card_subtitle_shows_contest_place_and_score():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="Ruby ", user_id="u"),
    ]})
    # The logger sits 5th on the current contest's leaderboard with 320.5 points.
    tadoku_client.get_contest_leaderboard.return_value = {
        "entries": [{"rank": 5, "user_display_name": "ruby", "score": 320.5, "is_tie": False}],
        "total_size": 1,
    }
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert profile_card.render_card.await_args.kwargs["subtitle"] == "#5 · 320.5 pts in 2026 Round 4"


async def test_poll_card_subtitle_blank_when_logger_not_on_leaderboard():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u"),
    ]})
    # Default fixture leaderboard is empty -> no entry -> blank subtitle.
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert profile_card.render_card.await_args.kwargs["subtitle"] == ""


async def test_poll_card_draws_the_material_title_on_the_card():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u", description="奇跡を、生きている"),
    ]})
    tadoku_client.list_user_logs.return_value = {"logs": [], "total_size": 0}
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # Title goes to the renderer (drawn on the card), not the message content.
    assert profile_card.render_card.await_args.kwargs["title"] == "奇跡を、生きている"
    assert channel.send.await_args.args[0] is None  # no message content
    assert isinstance(_sent(channel)["file"], discord.File)


async def test_poll_card_fetches_and_passes_poster_for_tagged_log(monkeypatch):
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    log = _log("2026-07-05T21:00:00Z", name="ruby", user_id="u", description="Summer Pockets")
    log["tags"] = ["fiction", "game"]
    tadoku_client.list_contest_logs.side_effect = _pager({0: [log]})
    tadoku_client.list_user_logs.return_value = {"logs": [], "total_size": 0}
    fetch = AsyncMock(return_value=b"POSTERBYTES")
    monkeypatch.setattr(log_feed.poster_client, "fetch_poster", fetch)
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # The poster lookup got the log's tags + description, and the bytes reached the card.
    fetch.assert_awaited_once()
    assert fetch.await_args.args[1] == ["fiction", "game"]
    assert fetch.await_args.args[2] == "Summer Pockets"
    assert profile_card.render_card.await_args.kwargs["poster_bytes"] == b"POSTERBYTES"


async def test_poll_card_tolerates_poster_lookup_failure(monkeypatch):
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    log = _log("2026-07-05T21:00:00Z", name="ruby", user_id="u", description="X")
    log["tags"] = ["anime"]
    tadoku_client.list_contest_logs.side_effect = _pager({0: [log]})
    tadoku_client.list_user_logs.return_value = {"logs": [], "total_size": 0}
    monkeypatch.setattr(
        log_feed.poster_client, "fetch_poster", AsyncMock(side_effect=RuntimeError("boom"))
    )
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    # Still an image card; the renderer just gets no poster.
    assert isinstance(_sent(channel)["file"], discord.File)
    assert profile_card.render_card.await_args.kwargs["poster_bytes"] is None


async def test_poll_plain_embed_for_unclaimed_logger():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)  # unclaimed short-circuits before any user/lifetime lookup
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="nobody", user_id="u"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    sent = _sent(channel)
    assert sent.get("file") is None
    assert sent["embed"].author.icon_url is None
    profile_card.render_card.assert_not_awaited()
    tadoku_client.list_user_logs.assert_not_awaited()


async def test_poll_posts_youtube_url_under_the_card():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="nobody", user_id="u",
             tags=["youtube"], description="すごい動画 https://youtu.be/xyz"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # Two messages: the card, then the link beneath it.
    assert channel.send.await_count == 2
    assert channel.send.await_args_list[0].kwargs.get("embed") is not None  # the card
    assert channel.send.await_args_list[1].args[0] == "https://youtu.be/xyz"  # the URL


async def test_poll_posts_all_youtube_urls_in_one_follow_up():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="nobody", user_id="u",
             tags=["youtube"], description="https://youtu.be/one https://youtu.be/two"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    # Card + a single follow-up carrying both links (one per line).
    assert channel.send.await_count == 2
    assert channel.send.await_args_list[1].args[0] == "https://youtu.be/one\nhttps://youtu.be/two"


async def test_poll_no_url_message_for_non_youtube_log():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", tags=["video"], description="title https://youtu.be/x"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_count == 1  # just the card; no link follow-up


async def test_poll_falls_back_to_embed_when_contest_stats_lookup_fails():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u"),
    ]})
    tadoku_client.list_user_logs.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    sent = _sent(channel)
    assert sent.get("file") is None
    assert sent["embed"] is not None  # plain embed fallback
    profile_card.render_card.assert_not_awaited()


async def test_poll_fetches_avatar_via_fetch_user_and_only_once():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: None
    bot.fetch_user = AsyncMock(return_value=_user_with_avatar(b"AV"))
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:05:00Z", name="ruby", user_id="u"),
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    assert channel.send.await_count == 2  # both logs posted as cards
    bot.fetch_user.assert_awaited_once()  # avatar resolved once (cached across the burst)


async def test_poll_card_uses_placeholder_when_avatar_read_fails():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    failing = SimpleNamespace(display_avatar=SimpleNamespace(read=AsyncMock(side_effect=_http_error())))
    bot.get_user = lambda uid: failing if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u"),
    ]})
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    # Still an image card; the renderer just gets no avatar bytes.
    assert isinstance(_sent(channel)["file"], discord.File)
    assert profile_card.render_card.await_args.kwargs["avatar_bytes"] is None


async def test_poll_falls_back_to_embed_when_card_render_raises(monkeypatch):
    # A render crash for one log must NOT freeze the guild's feed: it degrades to
    # the plain embed, the poll completes, and last_seen still advances.
    monkeypatch.setattr(profile_card, "render_card", AsyncMock(side_effect=ValueError("boom")))
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar() if uid == 111 else None
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="u"),
    ]})
    tadoku_client.list_user_logs.return_value = {"logs": [], "total_size": 0}
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)  # must not raise

    sent = _sent(channel)
    assert sent.get("file") is None
    assert sent["embed"] is not None  # degraded to the plain embed
    # Poll completed, so the high-water mark advanced (feed not frozen).
    assert config_store.get_guild_logfeed(999)["last_seen"] == "2026-07-05T21:00:00Z"


# ---------------------------------------------------------------------------
# contest-scoped stats (_compute_contest_totals)
# ---------------------------------------------------------------------------

# A timestamp comfortably inside CONTEST's window (2026-07-01 .. 2026-07-31).
IN_WINDOW = "2026-07-05T20:00:00Z"


def _ulog(unit, amount, deleted=False, created_at=IN_WINDOW, *, activity=None,
          duration_seconds=None):
    return {"unit_name": unit, "amount": amount, "deleted": deleted, "created_at": created_at,
            "activity": activity, "duration_seconds": duration_seconds}


async def test_compute_contest_totals_sums_by_unit_and_skips_deleted():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Character", 1000),
            _ulog("Page", 10),
            _ulog("Comic page", 5),          # counts as comic pages, not pages
            _ulog("Minute", 30),
            _ulog("Dense minute", 60),        # listening
            _ulog("Sentence", 99),            # ignored (not requested)
            _ulog("Character", 500, deleted=True),  # skipped
        ],
        "total_size": 7,
    }

    stats = await cog._compute_contest_totals("user-1", CONTEST)

    assert stats == {"characters": 1000, "pages": 10, "comic_pages": 5, "minutes": 90}


async def test_compute_contest_totals_combines_legacy_and_time_tracked_listening():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Minute", 30, activity="Listening"),
            _ulog("", 0, activity="Listening", duration_seconds=5_400),
            # If both are present, duration is the source of truth for this log.
            _ulog("Minute", 999, activity="Listening", duration_seconds=1_800),
            # Other activities' time/minute tracking does not belong in Listening.
            _ulog("", 0, activity="Reading", duration_seconds=3_600),
            _ulog("Minute", 45, activity="Study"),
        ],
        "total_size": 5,
    }

    stats = await cog._compute_contest_totals("user-1", CONTEST)

    assert stats == {"characters": 0, "pages": 0, "comic_pages": 0, "minutes": 150}


async def test_compute_contest_totals_stops_before_contest_start():
    # Newest-first: an in-window log, then one from before the contest which ends
    # the walk (so a still-older in-window-looking entry after it is never counted).
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Page", 10, created_at="2026-07-05T00:00:00Z"),
            _ulog("Page", 999, created_at="2026-06-30T23:59:59Z"),  # before start -> stop
            _ulog("Page", 999, created_at="2026-07-10T00:00:00Z"),  # never reached
        ],
        "total_size": 3,
    }

    stats = await cog._compute_contest_totals("user-1", CONTEST)

    assert stats == {"characters": 0, "pages": 10, "comic_pages": 0, "minutes": 0}


async def test_compute_contest_totals_skips_logs_after_contest_end():
    # Newest-first: a log after the contest is skipped (not counted), an in-window
    # log is summed, then a pre-contest log ends the walk. The final day (07-31)
    # counts in full; 08-01 does not.
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Page", 999, created_at="2026-08-01T00:00:00Z"),  # after end -> skip
            _ulog("Page", 7, created_at="2026-07-31T23:00:00Z"),    # last day -> counts
            _ulog("Page", 10, created_at="2026-07-15T00:00:00Z"),   # in window
            _ulog("Page", 999, created_at="2026-06-15T00:00:00Z"),  # before start -> stop
        ],
        "total_size": 4,
    }

    stats = await cog._compute_contest_totals("user-1", CONTEST)

    assert stats == {"characters": 0, "pages": 17, "comic_pages": 0, "minutes": 0}


async def test_compute_contest_totals_pages_until_total_reached():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    full = [_ulog("Page", 1) for _ in range(log_feed.LOG_PAGE_SIZE)]
    pages = {
        0: {"logs": full, "total_size": log_feed.LOG_PAGE_SIZE + 1},
        1: {"logs": [_ulog("Page", 1)], "total_size": log_feed.LOG_PAGE_SIZE + 1},
    }

    async def serve(session, user_id, *, page, page_size):
        return pages[page]

    tadoku_client.list_user_logs.side_effect = serve

    stats = await cog._compute_contest_totals("user-1", CONTEST)

    assert stats["pages"] == log_feed.LOG_PAGE_SIZE + 1
    assert tadoku_client.list_user_logs.await_count == 2


async def test_compute_window_totals_open_ended_end_counts_through_latest():
    # end=None -> no upper bound: every log at/after start counts, and the walk
    # still stops at the first log before start.
    from datetime import datetime, timezone
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Page", 5, created_at="2026-09-01T00:00:00Z"),   # well after start -> counts
            _ulog("Page", 10, created_at="2026-07-15T00:00:00Z"),  # in window -> counts
            _ulog("Page", 999, created_at="2026-06-01T00:00:00Z"),  # before start -> stop
        ],
        "total_size": 3,
    }

    stats = await log_feed.compute_window_totals(AsyncMock(), "user-1", start, None)

    assert stats == {"characters": 0, "pages": 15, "comic_pages": 0, "minutes": 0}


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


# ---------------------------------------------------------------------------
# rank-change callouts (_rank_callouts + the poll integration)
# ---------------------------------------------------------------------------

def _lb(*rows):
    """Leaderboard entries from ``(rank, user_id, name)`` triples."""
    return [{"rank": r, "user_id": uid, "user_display_name": name, "score": 100 - r, "is_tie": False}
            for r, uid, name in rows]


def test_rank_callouts_single_pass_names_the_rival():
    entries = _lb((1, "a", "anja"), (2, "b", "ruby"), (3, "c", "ryun"))
    old = {"a": 1, "b": 3, "c": 2}  # ruby 3->2, swapping past ryun
    out = log_feed._rank_callouts(entries, old, {"b"}, 20)
    assert [(e["user_display_name"], e["rank"], e["description"]) for e in out] == [
        ("ruby", 2, "Passed ryun"),
    ]


def test_rank_callouts_multi_pass_counts_the_others():
    entries = _lb((1, "a", "anja"), (2, "b", "ruby"), (3, "c", "ryun"))
    old = {"a": 1, "b": 4, "c": 2}  # ruby climbed 4->2
    out = log_feed._rank_callouts(entries, old, {"b"}, 20)
    assert out[0]["description"] == "Passed ryun and 1 other"  # passed 2; rival below is ryun


def test_rank_callouts_break_into_the_top_slice():
    entries = _lb((1, "a", "anja"), (2, "b", "ruby"))
    old = {"a": 1}  # ruby wasn't in the slice before
    assert log_feed._rank_callouts(entries, old, {"b"}, 20)[0]["description"] == "Broke into the top 20"


def test_rank_callouts_ignore_non_loggers_and_drops():
    entries = _lb((1, "a", "anja"), (2, "b", "ruby"))
    old = {"a": 2, "b": 1}  # anja climbed but didn't log; ruby dropped
    assert log_feed._rank_callouts(entries, old, {"b"}, 20) == []


def test_rank_callouts_are_ordered_best_move_first():
    entries = _lb((1, "a", "anja"), (2, "b", "ruby"), (3, "c", "ryun"))
    old = {"a": 1, "b": 5, "c": 6}  # ruby and ryun both climbed and logged
    out = log_feed._rank_callouts(entries, old, {"b", "c"}, 20)
    assert [e["rank"] for e in out] == [2, 3]


async def test_poll_posts_a_rank_up_card_when_a_logger_climbs():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    bot.get_user = lambda uid: _user_with_avatar(b"AV") if uid == 111 else None
    config_store.set_guild_logfeed(
        999, enabled=True, channel_id=555, last_seen=CUTOFF,
        rank_snapshot={"contest_id": "c1", "ranks": {"uuid-ruby": 5}},  # ruby was #5
    )
    config_store.set_claim(999, 111, "ruby")
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="uuid-ruby"),
    ]})
    tadoku_client.get_contest_leaderboard.return_value = {"entries": _lb(
        (1, "x", "top"), (2, "y", "anja"), (3, "uuid-ruby", "ruby"), (4, "z", "ryun"),
    ), "total_size": 4}
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    card = rank_card.render.await_args.args[0]
    assert (card.name, card.rank) == ("ruby", 3)
    assert card.description == "Passed ryun and 1 other"  # climbed 5 -> 3
    assert card.avatar == b"AV"
    # Snapshot advanced to the new positions.
    assert config_store.get_guild_logfeed(999)["rank_snapshot"]["ranks"]["uuid-ruby"] == 3


async def test_poll_first_baseline_seeds_snapshot_without_callouts():
    channel = _channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_logfeed(999, enabled=True, channel_id=555, last_seen=CUTOFF)  # no snapshot
    tadoku_client.list_contest_logs.side_effect = _pager({0: [
        _log("2026-07-05T21:00:00Z", name="ruby", user_id="uuid-ruby"),
    ]})
    tadoku_client.get_contest_leaderboard.return_value = {"entries": _lb(
        (1, "uuid-ruby", "ruby"), (2, "y", "anja"),
    ), "total_size": 2}
    cog = log_feed.LogFeed(bot)

    await cog._poll_guild(999)

    rank_card.render.assert_not_awaited()  # no baseline -> seed only
    assert config_store.get_guild_logfeed(999)["rank_snapshot"] == {
        "contest_id": "c1", "ranks": {"uuid-ruby": 1, "y": 2},
    }
