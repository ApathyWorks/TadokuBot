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
    # Stub the (Pillow) image renderer so poll tests don't render real PNGs; the
    # renderer itself is covered in test_profile_card.py.
    monkeypatch.setattr(profile_card, "render_card", AsyncMock(return_value=b"PNGDATA"))


def _log(created_at, name="ruby", score=10, deleted=False, activity="Reading",
         amount=5, unit="Page", language="Japanese", description=None, user_id=None, tags=None):
    return {
        "created_at": created_at, "user_display_name": name, "score": score, "deleted": deleted,
        "activity": activity, "amount": amount, "unit_name": unit,
        "language": language, "description": description, "user_id": user_id, "tags": tags,
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
    # Renderer got the logger's name, avatar bytes, lifetime stats and this-log line.
    kwargs = profile_card.render_card.await_args.kwargs
    assert kwargs["display_name"] == "Ruby"
    assert kwargs["avatar_bytes"] == b"AV"
    assert kwargs["characters"] == 6_600_000
    assert "Reading" in kwargs["this_log"] and "192 Page" in kwargs["this_log"]
    # And it summed the right user's history.
    assert tadoku_client.list_user_logs.await_args.args[1] == "uuid-ruby"


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


async def test_poll_falls_back_to_embed_when_lifetime_lookup_fails():
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
# stats since 2026 (_compute_lifetime)
# ---------------------------------------------------------------------------

# A timestamp comfortably inside the window (>= 2026-01-01).
IN_WINDOW = "2026-07-05T20:00:00Z"


def _ulog(unit, amount, deleted=False, created_at=IN_WINDOW):
    return {"unit_name": unit, "amount": amount, "deleted": deleted, "created_at": created_at}


async def test_compute_lifetime_sums_by_unit_and_skips_deleted():
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

    stats = await cog._compute_lifetime("user-1")

    assert stats == {"characters": 1000, "pages": 10, "comic_pages": 5, "minutes": 90}


async def test_compute_lifetime_stops_before_2026():
    # Newest-first: an in-window log, then one from 2025 which ends the walk (so a
    # still-older in-window-looking entry after it is never counted).
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    tadoku_client.list_user_logs.return_value = {
        "logs": [
            _ulog("Page", 10, created_at="2026-01-02T00:00:00Z"),
            _ulog("Page", 999, created_at="2025-12-31T23:59:59Z"),  # before 2026 -> stop
            _ulog("Page", 999, created_at="2026-05-01T00:00:00Z"),  # never reached
        ],
        "total_size": 3,
    }

    stats = await cog._compute_lifetime("user-1")

    assert stats == {"characters": 0, "pages": 10, "comic_pages": 0, "minutes": 0}


async def test_compute_lifetime_pages_until_total_reached():
    cog = log_feed.LogFeed(SimpleNamespace(session=AsyncMock()))
    full = [_ulog("Page", 1) for _ in range(log_feed.LOG_PAGE_SIZE)]
    pages = {
        0: {"logs": full, "total_size": log_feed.LOG_PAGE_SIZE + 1},
        1: {"logs": [_ulog("Page", 1)], "total_size": log_feed.LOG_PAGE_SIZE + 1},
    }

    async def serve(session, user_id, *, page, page_size):
        return pages[page]

    tadoku_client.list_user_logs.side_effect = serve

    stats = await cog._compute_lifetime("user-1")

    assert stats["pages"] == log_feed.LOG_PAGE_SIZE + 1
    assert tadoku_client.list_user_logs.await_count == 2


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
