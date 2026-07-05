"""Tests for the alerts cog (the /alerts command group + the scheduler).

The tadoku-facing embed build is covered in test_leaderboard_cog.py, so here we
monkeypatch ``build_period_leaderboard_embed`` / ``build_yearend_embed`` and
focus on the alert-specific logic: period/window computation, the ``/alerts``
on/off/status toggle (which drives all three kinds), and the scheduler's "post
once per period, retry on API error, tolerate a bad channel" behaviour. Cog
callbacks are invoked directly with a fake interaction; the scheduler is driven
through ``_run_due_alerts`` / ``_maybe_post`` / ``_post`` with a fixed ``now`` so
nothing depends on the wall clock or a live loop.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.alerts as alerts_cog
import cogs.leaderboard as leaderboard_cog
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTEST = {"id": "c1", "title": "2026 Round 4"}


def _fake_channel(cid=555, mention="#general", can_send=True):
    perms = SimpleNamespace(send_messages=can_send)
    return SimpleNamespace(
        id=cid, mention=mention, send=AsyncMock(), permissions_for=lambda member: perms
    )


def _bot_with_channel(channel):
    """A minimal bot whose get_channel returns ``channel`` for its own id."""
    bot = SimpleNamespace(session=AsyncMock())
    bot.get_channel = lambda cid, ch=channel: ch if (ch is not None and cid == ch.id) else None
    bot.fetch_channel = AsyncMock()
    return bot


def _http_error():
    """A discord.HTTPException instance for exercising the error branches."""
    return discord.HTTPException(SimpleNamespace(status=503, reason="err"), "boom")


@pytest.fixture
def patched_embed(monkeypatch):
    """Make build_period_leaderboard_embed return a ready embed by default."""
    builder = AsyncMock(return_value=(CONTEST, discord.Embed(title="wrap")))
    monkeypatch.setattr(leaderboard_cog, "build_period_leaderboard_embed", builder)
    return builder


# ---------------------------------------------------------------------------
# _period_key / _window_for
# ---------------------------------------------------------------------------


def test_period_key_weekly_uses_iso_year_week():
    now = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    iso = now.isocalendar()
    assert alerts_cog._period_key("weekly", now) == [iso[0], iso[1]]


def test_period_key_monthly_uses_year_month():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert alerts_cog._period_key("monthly", now) == [2026, 7]


def test_period_key_yearly_uses_year():
    now = datetime(2026, 12, 31, 23, tzinfo=timezone.utc)
    assert alerts_cog._period_key("yearly", now) == [2026]


def test_window_for_weekly_is_rolling_last_seven_days():
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    cutoff, until, title, phrase = alerts_cog._window_for("weekly", now)

    assert cutoff == now - timedelta(days=leaderboard_cog.WEEKLY_WINDOW_DAYS)
    assert until is None
    assert title == "last 7 days"
    assert phrase == "the last 7 days"


def test_window_for_monthly_is_the_previous_month():
    now = datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc)
    cutoff, until, title, phrase = alerts_cog._window_for("monthly", now)

    assert cutoff == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert until == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert title == "June 2026"
    assert phrase == "June 2026"


def test_window_for_monthly_in_january_rolls_back_to_december():
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    cutoff, until, title, _ = alerts_cog._window_for("monthly", now)

    assert cutoff == datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert until == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert title == "December 2025"


# ---------------------------------------------------------------------------
# /alerts on|off|status (one switch for all kinds)
# ---------------------------------------------------------------------------


def _guild_interaction(guild_id=999):
    interaction = make_interaction(guild_id=guild_id)
    interaction.guild = SimpleNamespace(me=object())  # for channel.permissions_for
    return interaction


async def test_alerts_on_enables_all_kinds_in_one_channel(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()
    interaction.channel = _fake_channel(cid=555)

    await cog.alerts_on.callback(cog, interaction, channel=None)

    now = datetime.now(timezone.utc)
    for kind in config_store.ALERT_KINDS:
        settings = config_store.get_guild_alert(999, kind)
        assert settings["enabled"] is True
        assert settings["channel_id"] == 555
        # Seeded to the current period so the first post lands at the next boundary.
        assert settings["last_period"] == alerts_cog._period_key(kind, now)
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert "on" in args[0]


async def test_alerts_on_uses_explicit_channel_over_current(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()
    interaction.channel = _fake_channel(cid=1)  # should be ignored
    chosen = _fake_channel(cid=777, mention="#logs")

    await cog.alerts_on.callback(cog, interaction, channel=chosen)

    for kind in config_store.ALERT_KINDS:
        assert config_store.get_guild_alert(999, kind)["channel_id"] == 777


async def test_alerts_on_refuses_channel_it_cannot_post_to(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()
    interaction.channel = _fake_channel(cid=555, can_send=False)

    await cog.alerts_on.callback(cog, interaction, channel=None)

    # Nothing enabled, and the reply explains why.
    for kind in config_store.ALERT_KINDS:
        assert config_store.get_guild_alert(999, kind)["enabled"] is False
    args, _ = interaction.response.send_message.await_args
    assert "permission" in args[0].lower()


async def test_alerts_off_disables_all_kinds(fake_bot):
    for kind in config_store.ALERT_KINDS:
        config_store.set_guild_alert(999, kind, enabled=True, channel_id=555)
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()

    await cog.alerts_off.callback(cog, interaction)

    for kind in config_store.ALERT_KINDS:
        assert config_store.get_guild_alert(999, kind)["enabled"] is False
    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


async def test_alerts_status_reports_on(fake_bot):
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=555)
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()

    await cog.alerts_status.callback(cog, interaction)

    args, kwargs = interaction.response.send_message.await_args
    assert "on" in args[0]
    assert "555" in args[0]  # rendered as the <#555> channel mention
    assert kwargs.get("ephemeral") is True


async def test_alerts_status_reports_off(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = _guild_interaction()

    await cog.alerts_status.callback(cog, interaction)

    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


def test_alerts_group_requires_manage_guild():
    perms = alerts_cog.Alerts.alerts_group.default_permissions
    assert perms is not None and perms.manage_guild is True


# ---------------------------------------------------------------------------
# scheduler: _maybe_post
# ---------------------------------------------------------------------------


async def test_maybe_post_weekly_posts_when_period_advanced(patched_embed):
    channel = _fake_channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=555, last_period=[2020, 1])
    cog = alerts_cog.Alerts(bot)
    now = datetime(2026, 7, 6, 0, 30, tzinfo=timezone.utc)  # a Monday

    await cog._maybe_post(999, "weekly", now)

    channel.send.assert_awaited_once()
    assert "embed" in channel.send.await_args.kwargs
    assert config_store.get_guild_alert(999, "weekly")["last_period"] == alerts_cog._period_key("weekly", now)
    # Weekly window: rolling last 7 days, open-ended.
    bkwargs = patched_embed.await_args.kwargs
    assert bkwargs["until"] is None
    assert bkwargs["title_suffix"] == "last 7 days"


async def test_maybe_post_skips_when_already_posted_this_period(patched_embed):
    channel = _fake_channel()
    bot = _bot_with_channel(channel)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    period = alerts_cog._period_key("weekly", now)
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=channel.id, last_period=period)
    cog = alerts_cog.Alerts(bot)

    await cog._maybe_post(999, "weekly", now)

    channel.send.assert_not_awaited()
    patched_embed.assert_not_awaited()


async def test_maybe_post_skips_when_disabled(patched_embed):
    channel = _fake_channel()
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "weekly", enabled=False, channel_id=channel.id)
    cog = alerts_cog.Alerts(bot)

    await cog._maybe_post(999, "weekly", datetime(2026, 7, 6, tzinfo=timezone.utc))

    channel.send.assert_not_awaited()
    patched_embed.assert_not_awaited()


async def test_maybe_post_monthly_posts_previous_month(patched_embed):
    channel = _fake_channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "monthly", enabled=True, channel_id=555, last_period=[2026, 6])
    cog = alerts_cog.Alerts(bot)
    now = datetime(2026, 7, 1, 0, 15, tzinfo=timezone.utc)

    await cog._maybe_post(999, "monthly", now)

    channel.send.assert_awaited_once()
    bkwargs = patched_embed.await_args.kwargs
    assert bkwargs["cutoff"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert bkwargs["until"] == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert bkwargs["title_suffix"] == "June 2026"
    assert config_store.get_guild_alert(999, "monthly")["last_period"] == [2026, 7]


async def test_maybe_post_yearly_posts_final_standings(monkeypatch):
    channel = _fake_channel(cid=555)
    bot = _bot_with_channel(channel)
    # Yearly uses the cumulative-standings builder, not the period one.
    yearend = AsyncMock(return_value=(CONTEST, discord.Embed(title="final")))
    monkeypatch.setattr(leaderboard_cog, "build_yearend_embed", yearend)
    config_store.set_guild_alert(999, "yearly", enabled=True, channel_id=555, last_period=[2025])
    cog = alerts_cog.Alerts(bot)
    now = datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc)

    await cog._maybe_post(999, "yearly", now)

    channel.send.assert_awaited_once()
    yearend.assert_awaited_once_with(bot, 999)
    assert config_store.get_guild_alert(999, "yearly")["last_period"] == [2026]


async def test_maybe_post_marks_done_when_nothing_to_post(monkeypatch):
    monkeypatch.setattr(
        leaderboard_cog, "build_period_leaderboard_embed", AsyncMock(return_value=(CONTEST, None))
    )
    channel = _fake_channel()
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=channel.id, last_period=[2020, 1])
    cog = alerts_cog.Alerts(bot)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)

    await cog._maybe_post(999, "weekly", now)

    channel.send.assert_not_awaited()
    # Marker still advances so an empty period isn't re-checked every hour.
    assert config_store.get_guild_alert(999, "weekly")["last_period"] == alerts_cog._period_key("weekly", now)


async def test_maybe_post_does_not_advance_marker_on_api_error(monkeypatch):
    monkeypatch.setattr(
        leaderboard_cog,
        "build_period_leaderboard_embed",
        AsyncMock(side_effect=tadoku_client.TadokuAPIError("boom")),
    )
    channel = _fake_channel()
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=channel.id, last_period=[2020, 1])
    cog = alerts_cog.Alerts(bot)

    await cog._maybe_post(999, "weekly", datetime(2026, 7, 6, tzinfo=timezone.utc))

    channel.send.assert_not_awaited()
    # Left unchanged so the next tick retries.
    assert config_store.get_guild_alert(999, "weekly")["last_period"] == [2020, 1]


# ---------------------------------------------------------------------------
# scheduler: _run_due_alerts
# ---------------------------------------------------------------------------


async def test_run_due_alerts_posts_for_all_enabled_kinds(patched_embed):
    channel = _fake_channel(cid=555)
    bot = _bot_with_channel(channel)
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=555, last_period=[2020, 1])
    config_store.set_guild_alert(999, "monthly", enabled=True, channel_id=555, last_period=[2020, 1])
    cog = alerts_cog.Alerts(bot)

    await cog._run_due_alerts(datetime(2026, 7, 6, tzinfo=timezone.utc))

    assert channel.send.await_count == 2


async def test_run_due_alerts_isolates_a_failing_guild(monkeypatch):
    # A non-API error inside one post must not stop the loop from finishing.
    monkeypatch.setattr(
        leaderboard_cog,
        "build_period_leaderboard_embed",
        AsyncMock(side_effect=RuntimeError("kaboom")),
    )
    bot = _bot_with_channel(_fake_channel())
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=555, last_period=[2020, 1])
    cog = alerts_cog.Alerts(bot)

    # Should not raise.
    await cog._run_due_alerts(datetime(2026, 7, 6, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# scheduler: _post
# ---------------------------------------------------------------------------


async def test_post_sends_to_configured_channel():
    channel = _fake_channel(cid=555)
    bot = _bot_with_channel(channel)
    cog = alerts_cog.Alerts(bot)
    embed = discord.Embed(title="x")

    await cog._post(999, 555, embed)

    channel.send.assert_awaited_once_with(embed=embed)


async def test_post_is_a_noop_when_channel_id_is_none():
    bot = _bot_with_channel(None)
    cog = alerts_cog.Alerts(bot)

    await cog._post(999, None, discord.Embed())  # must not raise


async def test_post_swallows_send_errors():
    channel = _fake_channel(cid=555)
    channel.send = AsyncMock(side_effect=_http_error())
    bot = _bot_with_channel(channel)
    cog = alerts_cog.Alerts(bot)

    await cog._post(999, 555, discord.Embed())  # must not raise


async def test_post_handles_a_missing_channel():
    bot = SimpleNamespace(session=AsyncMock())
    bot.get_channel = lambda cid: None
    bot.fetch_channel = AsyncMock(side_effect=_http_error())
    cog = alerts_cog.Alerts(bot)

    await cog._post(999, 555, discord.Embed())  # must not raise
