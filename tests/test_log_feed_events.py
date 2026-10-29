"""End-to-end scenarios for the log feed's event-stream sync.

Each test drives the feed the way the running bot does -- admin commands, then
``LogFeed._run_poll()`` ticks -- against ``FakeContestFeed``, a loopback
stand-in for tadoku.app reached through the real ``lib.tadoku_client`` HTTP
path, and ``FakeChannel``, which records every Discord send / edit / delete.

Mocked boundaries: Discord itself (``FakeChannel``) and the Pillow card
renderers (stubbed here; covered by their own tests).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

import cogs.log_feed as log_feed
import lib.config_store as config_store
import lib.profile_card as profile_card
import lib.rank_card as rank_card
from tests.conftest import make_interaction

GUILD = 999
CHANNEL = 555
CONTEST = {"id": "c1", "title": "2026 Round 4",
           "contest_start": "2026-01-01", "contest_end": "2026-12-31"}


class FakeContestFeed:
    """tadoku.app's view of one contest: its ordered event feed, and each log's
    current state (a log missing from ``logs`` is not in the contest -> 404)."""

    def __init__(self, server, contest):
        self.server = server
        self.contest_id = contest["id"]
        self.events: list[dict] = []
        self.logs: dict[str, dict] = {}
        self.events_status = 200            # force an error from the events endpoint
        self.log_status: dict[str, int] = {}  # force an error for one log's fetch
        server.set_response(f"/contests/{self.contest_id}", 200, contest)
        server.set_response(f"/contests/{self.contest_id}/events", self._events)

    def _events(self, query):
        if self.events_status != 200:
            return self.events_status, {"error": "nope"}
        cursor, limit = query["cursor"], int(query.get("limit", 100))
        ids = [event["id"] for event in self.events]
        if cursor == "now":
            return 200, {"events": [], "next_cursor": ids[-1] if ids else "0", "has_more": False}
        if cursor == "0":
            start = 0
        elif cursor in ids:
            start = ids.index(cursor) + 1
        else:
            return 400, {"error": "invalid cursor"}
        page = self.events[start:start + limit]
        return 200, {
            "events": page,
            "next_cursor": page[-1]["id"] if page else cursor,
            "has_more": start + limit < len(self.events),
        }

    def _log(self, log_id):
        if log_id in self.log_status:
            return self.log_status[log_id], {}
        if log_id not in self.logs:
            return 404, {"error": "not found"}
        return 200, self.logs[log_id]

    def emit(self, kind: str, log_id: str) -> str:
        event_id = f"{self.contest_id}-e{len(self.events) + 1}"
        self.events.append({"id": event_id, "log_id": log_id, "event": kind,
                            "occurred_at": "2026-10-24T12:00:00Z"})
        self.server.set_response(
            f"/contests/{self.contest_id}/logs/{log_id}", lambda query, lid=log_id: self._log(lid)
        )
        return event_id

    # What a Tadoku user (or moderator) does, as seen by the contest.
    def submit(self, log: dict) -> str:
        self.logs[log["id"]] = log
        return self.emit("log.submitted", log["id"])

    def update(self, log_id: str, **changes) -> str:
        self.logs[log_id] = {**self.logs[log_id], **changes}
        return self.emit("log.updated", log_id)

    def withdraw(self, log_id: str) -> str:
        self.logs.pop(log_id, None)
        return self.emit("log.withdrawn", log_id)

    @property
    def last_event_id(self) -> str:
        return self.events[-1]["id"]


def _not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class FakeChannel:
    """A Discord text channel recording every send / edit / delete the bot makes."""

    def __init__(self, cid=CHANNEL):
        self.id = cid
        self.mention = f"<#{cid}>"
        self.timeline: list[tuple] = []
        self.live: dict[int, dict] = {}  # message id -> latest payload
        self._next_id = 1000

    def permissions_for(self, member):
        return SimpleNamespace(send_messages=True)

    async def send(self, content=None, *, embed=None, file=None, suppress_embeds=False):
        self._next_id += 1
        payload = {"content": content, "embed": embed, "file": file, "suppress": suppress_embeds}
        self.live[self._next_id] = payload
        self.timeline.append(("send", self._next_id, payload))
        return FakeMessage(self, self._next_id)

    def get_partial_message(self, message_id):
        return FakeMessage(self, message_id)

    async def fetch_message(self, message_id):
        if message_id not in self.live:
            raise _not_found()
        return FakeMessage(self, message_id)

    def ops(self) -> list[tuple]:
        """The timeline as ``(op, message_id)`` pairs, then cleared."""
        ops = [(entry[0], entry[1]) for entry in self.timeline]
        self.timeline.clear()
        return ops


class FakeMessage:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def edit(self, **changes):
        if self.id not in self.channel.live:
            raise _not_found()
        self.channel.live[self.id].update(changes)
        self.channel.timeline.append(("edit", self.id, changes))

    async def delete(self):
        if self.channel.live.pop(self.id, None) is None:
            raise _not_found()
        self.channel.timeline.append(("delete", self.id))


@pytest.fixture
async def world(tadoku_server, monkeypatch):
    monkeypatch.setattr(profile_card, "render_card", AsyncMock(return_value=b"PNGDATA"))
    monkeypatch.setattr(rank_card, "render", AsyncMock(return_value=b"RANKPNG"))
    tadoku_server.set_response("/contests/latest-official", 200, CONTEST)
    feed = FakeContestFeed(tadoku_server, CONTEST)
    channel = FakeChannel()
    async with aiohttp.ClientSession() as session:
        bot = SimpleNamespace(
            session=session,
            get_channel=lambda cid: channel if cid == channel.id else None,
            fetch_channel=AsyncMock(side_effect=_not_found()),
            get_user=lambda uid: None,
            fetch_user=AsyncMock(side_effect=_not_found()),
        )
        yield SimpleNamespace(
            server=tadoku_server, feed=feed, channel=channel, bot=bot, cog=log_feed.LogFeed(bot)
        )


def _log(log_id, name="nobody", score=10, description=None, tags=None, user_id=None):
    return {
        "id": log_id, "user_display_name": name, "user_id": user_id or f"u-{name}",
        "score": score, "activity": {"id": 1, "name": "Reading"},
        "language": {"code": "jpn", "name": "Japanese"}, "amount": 5, "unit_name": "Page",
        "description": description, "tags": tags or [],
        "created_at": "2026-10-24T12:00:00Z", "deleted": False,
    }


def _interaction(channel):
    interaction = make_interaction(guild_id=GUILD)
    interaction.guild = SimpleNamespace(me=object())
    interaction.channel = channel
    return interaction


async def _log_on(w):
    await log_feed.LogFeed.log_on.callback(w.cog, _interaction(w.channel), channel=None)


async def _log_off(w):
    await log_feed.LogFeed.log_off.callback(w.cog, _interaction(w.channel))


async def _tick(w):
    await w.cog._run_poll()


def _stream() -> dict:
    return config_store.get_guild_logfeed(GUILD).get("stream") or {}


async def _started(w):
    """Feed on in CHANNEL, and the first tick has seeded its cursor."""
    config_store.set_guild_logfeed(GUILD, enabled=True, channel_id=CHANNEL)
    await _tick(w)
    w.channel.ops()


def _points(embed) -> str:
    return {field.name: field.value for field in embed.fields}["Points"]


# ---------------------------------------------------------------------------
# 1. seeding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("legacy", [False, True])
async def test_first_poll_seeds_cursor_at_now_and_posts_nothing(world, legacy):
    world.feed.submit(_log("before"))  # history from before the feed was on
    if legacy:
        # A config written by the old timestamp poller.
        config_store._write({str(GUILD): {"logfeed": {
            "enabled": True, "channel_id": CHANNEL, "last_seen": "2026-10-01T00:00:00Z",
        }}})
    else:
        config_store.set_guild_logfeed(GUILD, enabled=True, channel_id=CHANNEL)

    await _tick(world)

    assert world.channel.ops() == []
    assert _stream() == {"contest_id": "c1", "cursor": world.feed.last_event_id, "messages": {}}


# ---------------------------------------------------------------------------
# 2-5. one log's lifecycle
# ---------------------------------------------------------------------------

async def test_log_lifecycle_posts_edits_deletes_and_reposts(world):
    await _started(world)

    world.feed.submit(_log("l1", score=10, description="Vol 1 https://example.com/a"))
    await _tick(world)
    sends = world.channel.timeline[:]
    assert world.channel.ops() == [("send", 1001), ("send", 1002)]
    assert _points(sends[0][2]["embed"]) == "+10"
    assert sends[1][2]["content"] == "https://example.com/a"
    assert _stream().get("messages") == {"l1": {"channel_id": CHANNEL, "card": 1001, "links": 1002}}

    world.feed.update("l1", score=20, description="Vol 1 https://example.com/b")
    await _tick(world)
    edits = world.channel.timeline[:]
    assert world.channel.ops() == [("edit", 1001), ("edit", 1002)]
    assert _points(edits[0][2]["embed"]) == "+20"
    assert edits[0][2]["attachments"] == []
    assert edits[1][2]["content"] == "https://example.com/b"

    world.feed.withdraw("l1")
    await _tick(world)
    assert world.channel.ops() == [("delete", 1001), ("delete", 1002)]
    assert _stream().get("messages") == {}

    world.feed.submit(_log("l1", score=20, description="Vol 1 https://example.com/b"))
    await _tick(world)
    assert world.channel.ops() == [("send", 1003), ("send", 1004)]
    assert _stream().get("cursor") == world.feed.last_event_id


async def test_submission_already_withdrawn_is_not_posted(world):
    await _started(world)
    world.feed.submit(_log("l1"))
    world.feed.withdraw("l1")  # gone before the bot looked

    await _tick(world)

    assert world.channel.ops() == []
    assert _stream().get("cursor") == world.feed.last_event_id


async def test_withdrawn_event_for_a_resubmitted_log_keeps_the_card(world):
    await _started(world)
    world.feed.submit(_log("l1", score=10))
    await _tick(world)
    world.channel.ops()
    # Withdrawn and resubmitted before the next poll: it's in the contest now.
    world.feed.withdraw("l1")
    world.feed.submit(_log("l1", score=30))

    await _tick(world)

    timeline = world.channel.timeline[:]
    assert world.channel.ops() == [("edit", 1001), ("edit", 1001)]
    assert _points(timeline[-1][2]["embed"]) == "+30"
    assert "l1" in _stream().get("messages")


async def test_events_for_logs_never_posted_are_ignored(world):
    await _started(world)
    world.feed.logs["old"] = _log("old")  # submitted before the feed was on
    world.feed.emit("log.updated", "old")
    world.feed.emit("log.withdrawn", "old")

    await _tick(world)

    assert world.channel.ops() == []
    assert _stream().get("cursor") == world.feed.last_event_id


# ---------------------------------------------------------------------------
# 6-9. failures, replays, paging, bursts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [403, 500])
async def test_failed_log_fetch_retries_later_and_never_deletes(world, status):
    await _started(world)
    submitted = world.feed.submit(_log("l1", score=10))
    await _tick(world)
    world.channel.ops()
    world.feed.update("l1", score=20)
    world.feed.log_status["l1"] = status

    await _tick(world)

    assert world.channel.ops() == []  # no delete, no edit
    assert _stream().get("cursor") == submitted  # stopped before the failed event
    assert "l1" in _stream().get("messages")

    del world.feed.log_status["l1"]
    await _tick(world)

    assert world.channel.ops() == [("edit", 1001)]
    assert _stream().get("cursor") == world.feed.last_event_id


async def test_replayed_event_edits_instead_of_posting_twice(world):
    await _started(world)
    seed = _stream().get("cursor")
    world.feed.submit(_log("l1"))
    await _tick(world)
    assert world.channel.ops() == [("send", 1001)]
    # A crash lost the cursor save after the post: the event comes round again.
    config_store.set_guild_logfeed(GUILD, stream={**_stream(), "cursor": seed})

    await _tick(world)

    assert world.channel.ops() == [("edit", 1001)]


async def test_has_more_pages_are_read_in_the_same_tick(world):
    await _started(world)
    for i in range(120):  # more than one page of events the bot has nothing to do for
        world.feed.logs[f"x{i}"] = _log(f"x{i}")
        world.feed.emit("log.updated", f"x{i}")
    world.feed.submit(_log("l1"))  # lands on the second page

    await _tick(world)

    assert world.channel.ops() == [("send", 1001)]
    assert _stream().get("cursor") == world.feed.last_event_id


async def test_burst_posts_up_to_the_cap_then_resumes_next_tick(world):
    await _started(world)
    event_ids = [world.feed.submit(_log(f"l{i}", name=f"u{i}")) for i in range(25)]

    await _tick(world)

    first = world.channel.timeline[:]
    assert [op for op, _ in world.channel.ops()] == ["send"] * log_feed.MAX_POSTS_PER_POLL
    assert all(entry[2]["content"] is None for entry in first)  # no "…and N more" note
    assert _stream().get("cursor") == event_ids[log_feed.MAX_POSTS_PER_POLL - 1]

    await _tick(world)

    assert [op for op, _ in world.channel.ops()] == ["send"] * 5
    assert _stream().get("cursor") == event_ids[-1]
    assert len(_stream().get("messages")) == 25


# ---------------------------------------------------------------------------
# 10-11. contest switches and a broken feed
# ---------------------------------------------------------------------------

async def test_contest_change_reseeds_and_forgets_the_old_contests_messages(world):
    await _started(world)
    world.feed.submit(_log("l1"))
    await _tick(world)
    world.channel.ops()
    other = FakeContestFeed(world.server, {**CONTEST, "id": "c2", "title": "Other"})
    other.submit(_log("m0"))  # c2 history from before the switch
    config_store.set_guild_contest(GUILD, "c2", "Other")

    await _tick(world)

    assert world.channel.ops() == []
    assert _stream() == {"contest_id": "c2", "cursor": other.last_event_id, "messages": {}}

    other.submit(_log("m1"))
    world.feed.update("l1", score=99)  # the old contest is no longer followed
    await _tick(world)

    assert world.channel.ops() == [("send", 1002)]
    assert list(_stream().get("messages")) == ["m1"]


async def test_rejected_cursor_is_kept_not_reset_to_now(world):
    await _started(world)
    seed = _stream().get("cursor")
    world.feed.submit(_log("l1"))
    world.feed.events_status = 400

    await _tick(world)
    await _tick(world)

    assert world.channel.ops() == []
    assert _stream().get("cursor") == seed
    seeds = [query for path, query in world.server.requests
             if path == "/contests/c1/events" and query.get("cursor") == "now"]
    assert len(seeds) == 1  # only the original seed; never silently reset


# ---------------------------------------------------------------------------
# 12-15. what's already in Discord
# ---------------------------------------------------------------------------

async def test_card_deleted_by_hand_is_forgotten_not_reposted(world):
    await _started(world)
    world.feed.submit(_log("l1", description="https://example.com/a"))
    await _tick(world)
    assert world.channel.ops() == [("send", 1001), ("send", 1002)]
    world.channel.live.pop(1001)  # a moderator deleted the card

    world.feed.update("l1", score=20)
    await _tick(world)

    assert world.channel.ops() == [("delete", 1002)]  # its links go with it
    assert _stream().get("messages") == {}

    world.feed.update("l1", score=30)
    await _tick(world)
    assert world.channel.ops() == []


async def test_links_message_follows_description_and_nsfw_edits(world):
    await _started(world)
    world.feed.submit(_log("l1", description="Vol 1"))
    await _tick(world)
    assert world.channel.ops() == [("send", 1001)]

    world.feed.update("l1", description="Vol 1 https://a.example/x")
    await _tick(world)
    timeline = world.channel.timeline[:]
    assert world.channel.ops() == [("edit", 1001), ("send", 1002)]
    assert timeline[1][2]["content"] == "https://a.example/x"
    assert timeline[1][2]["suppress"] is False

    world.feed.update("l1", description="Vol 1 https://a.example/y", tags=["nsfw"])
    await _tick(world)
    timeline = world.channel.timeline[:]
    assert world.channel.ops() == [("edit", 1001), ("edit", 1002)]
    assert timeline[1][2] == {"content": "https://a.example/y", "suppress": True}

    world.feed.update("l1", description="Vol 1", tags=[])
    await _tick(world)
    assert world.channel.ops() == [("edit", 1001), ("delete", 1002)]
    assert _stream().get("messages")["l1"]["links"] is None


async def test_claiming_between_submit_and_update_switches_to_the_image_card(world):
    await _started(world)
    world.feed.submit(_log("l1", name="ruby"))
    await _tick(world)
    sent = world.channel.timeline[0][2] if world.channel.timeline else {}
    assert world.channel.ops() == [("send", 1001)]
    assert sent["embed"] is not None and sent["file"] is None

    config_store.set_claim(GUILD, 111, "ruby")
    world.bot.get_user = lambda uid: SimpleNamespace(
        display_avatar=SimpleNamespace(read=AsyncMock(return_value=b"AV")))
    world.server.set_response("/users/u-ruby/logs", 200, {"logs": [], "total_size": 0})
    world.feed.update("l1", score=20)
    await _tick(world)

    changes = world.channel.timeline[0][2] if world.channel.timeline else {}
    assert world.channel.ops() == [("edit", 1001)]
    assert changes["embed"] is None
    [file] = changes["attachments"]
    assert isinstance(file, discord.File)


async def test_unknown_event_types_are_skipped(world):
    await _started(world)
    skipped = world.feed.emit("log.reacted", "l9")

    await _tick(world)

    assert world.channel.ops() == []
    assert _stream().get("cursor") == skipped


# ---------------------------------------------------------------------------
# 16. rank-up callouts still ride along
# ---------------------------------------------------------------------------

async def test_rank_up_card_follows_a_submission_that_climbs(world):
    board = {"entries": [
        {"rank": 1, "user_id": "u-top", "user_display_name": "top", "score": 90, "is_tie": False},
        {"rank": 2, "user_id": "u-ryun", "user_display_name": "ryun", "score": 80, "is_tie": False},
        {"rank": 3, "user_id": "u-ruby", "user_display_name": "ruby", "score": 70, "is_tie": False},
    ]}
    world.server.set_response("/contests/c1/leaderboard", lambda query: (200, board))
    await _started(world)
    world.feed.submit(_log("l1", name="ruby"))
    await _tick(world)  # first baseline: snapshot only
    rank_card.render.assert_not_awaited()
    assert world.channel.ops() == [("send", 1001)]

    board["entries"][1:] = [
        {"rank": 2, "user_id": "u-ruby", "user_display_name": "ruby", "score": 85, "is_tie": False},
        {"rank": 3, "user_id": "u-ryun", "user_display_name": "ryun", "score": 80, "is_tie": False},
    ]
    world.feed.submit(_log("l2", name="ruby", score=15))
    await _tick(world)

    rank_card.render.assert_awaited_once()
    card = rank_card.render.await_args.args[0]
    assert (card.name, card.rank, card.description) == ("ruby", 2, "Passed ryun")
    timeline = world.channel.timeline[:]
    assert [op for op, _ in world.channel.ops()] == ["send", "send"]  # the log, then the rank-up
    assert timeline[1][2]["file"].filename == "rankup.png"


# ---------------------------------------------------------------------------
# 17. /log on|off around the stream
# ---------------------------------------------------------------------------

async def test_log_on_off_restart_from_now_but_keep_following_posted_cards(world):
    await _log_on(world)
    await _tick(world)  # seeds
    world.feed.submit(_log("l1", score=10))
    await _tick(world)
    assert world.channel.ops() == [("send", 1001)]

    await _log_off(world)
    stream_while_off = _stream()
    world.feed.submit(_log("l2"))  # logged while the feed is off
    await _tick(world)
    assert world.channel.ops() == []
    assert _stream() == stream_while_off

    await _log_on(world)  # off -> on: no backlog dump...
    assert _stream().get("cursor") is None
    await _tick(world)
    assert world.channel.ops() == []
    world.feed.update("l1", score=20)  # ...but cards already posted keep following their logs
    await _tick(world)
    assert world.channel.ops() == [("edit", 1001)]

    cursor = _stream().get("cursor")
    await _log_on(world)  # already on (e.g. to confirm the channel): keep the place
    assert _stream().get("cursor") == cursor
    assert "l1" in _stream().get("messages")


@pytest.mark.parametrize("operation", ["card_send", "links_send", "card_edit", "links_edit", "card_delete", "links_delete", "channel"])
async def test_discord_failures_keep_event_pending_until_recovered(world, monkeypatch, operation):
    await _started(world)
    log = _log("l1", description="https://example.com/old")
    if operation not in ("card_send", "links_send", "channel"):
        world.feed.submit(log)
        await _tick(world)
        world.channel.ops()
    cursor = _stream()["cursor"]
    if operation.endswith("delete"):
        world.feed.withdraw("l1")
    elif operation.endswith("edit"):
        world.feed.update("l1", score=25, description="https://example.com/new")
    else:
        world.feed.submit(log)

    error = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "temporary")
    failing = True
    send = world.channel.send
    edit = FakeMessage.edit
    delete = FakeMessage.delete

    async def flaky_send(content=None, **kwargs):
        if failing and ((operation == "card_send" and content is None) or
                        (operation == "links_send" and content is not None)):
            raise error
        return await send(content, **kwargs)

    async def flaky_edit(message, **kwargs):
        if failing and ((operation == "card_edit" and message.id == 1001) or
                        (operation == "links_edit" and message.id == 1002)):
            raise error
        return await edit(message, **kwargs)

    async def flaky_delete(message):
        if failing and ((operation == "card_delete" and message.id == 1001) or
                        (operation == "links_delete" and message.id == 1002)):
            raise error
        return await delete(message)

    monkeypatch.setattr(world.channel, "send", flaky_send)
    monkeypatch.setattr(FakeMessage, "edit", flaky_edit)
    monkeypatch.setattr(FakeMessage, "delete", flaky_delete)
    if operation == "channel":
        world.bot.get_channel = lambda cid: None
        world.bot.fetch_channel.side_effect = error

    await _tick(world)
    assert _stream()["cursor"] == cursor
    if operation == "links_send":
        # The card succeeded: checkpoint it before retrying its failed links send.
        assert _stream()["messages"]["l1"]["card"] == 1001

    failing = False
    world.bot.get_channel = lambda cid: world.channel
    await _tick(world)
    assert _stream()["cursor"] == world.feed.last_event_id
    if operation.endswith("delete"):
        assert world.channel.live == {}
        assert _stream()["messages"] == {}
    else:
        assert len(world.channel.live) == 2
        assert _stream()["messages"]["l1"] == {"channel_id": CHANNEL, "card": 1001, "links": 1002}
        card_sends = [entry for entry in world.channel.timeline if entry[0] == "send" and entry[2]["embed"]]
        assert len(card_sends) <= 1
