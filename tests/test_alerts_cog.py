"""Tests for the alerts cog (/weekly_wrapup, /monthly_wrapup + the scheduler).

The tadoku-facing embed build is covered in test_leaderboard_cog.py, so here we
monkeypatch ``build_period_leaderboard_embed`` and focus on the alert-specific
logic: period/window computation, the admin toggle commands, and the scheduler's
"post once per period, retry on API error, tolerate a bad channel" behaviour.
Cog callbacks are invoked directly with a fake interaction; the scheduler is
driven through ``_run_due_alerts`` / ``_maybe_post`` / ``_post`` with a fixed
``now`` so nothing depends on the wall clock or a live loop.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.app_commands import MissingPermissions

import cogs.alerts as alerts_cog
import cogs.leaderboard as leaderboard_cog
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTEST = {"id": "c1", "title": "2026 Round 4"}


def _fake_channel(cid=555, mention="#general"):
    return SimpleNamespace(id=cid, mention=mention, send=AsyncMock())


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
# /weekly_wrapup & /monthly_wrapup (admin toggles)
# ---------------------------------------------------------------------------


async def test_weekly_wrapup_enable_stores_channel_and_seeds_period(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.channel = _fake_channel(cid=555)

    await cog.weekly_wrapup.callback(cog, interaction, enabled=True, channel=None)

    settings = config_store.get_guild_alert(999, "weekly")
    assert settings["enabled"] is True
    assert settings["channel_id"] == 555
    # Seeded to the current period so the first post lands at the next boundary.
    assert settings["last_period"] == alerts_cog._period_key("weekly", datetime.now(timezone.utc))
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    assert "on" in args[0]


async def test_weekly_wrapup_enable_uses_explicit_channel_over_current(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.channel = _fake_channel(cid=1)  # should be ignored
    chosen = _fake_channel(cid=777, mention="#logs")

    await cog.weekly_wrapup.callback(cog, interaction, enabled=True, channel=chosen)

    assert config_store.get_guild_alert(999, "weekly")["channel_id"] == 777


async def test_weekly_wrapup_disable(fake_bot):
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=1)
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weekly_wrapup.callback(cog, interaction, enabled=False, channel=None)

    assert config_store.get_guild_alert(999, "weekly")["enabled"] is False
    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


async def test_weekly_wrapup_reports_state_when_on(fake_bot):
    config_store.set_guild_alert(999, "weekly", enabled=True, channel_id=555)
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weekly_wrapup.callback(cog, interaction, enabled=None, channel=None)

    args, kwargs = interaction.response.send_message.await_args
    assert "on" in args[0]
    assert "555" in args[0]  # rendered as the <#555> channel mention
    assert kwargs.get("ephemeral") is True


async def test_weekly_wrapup_reports_state_when_off(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.weekly_wrapup.callback(cog, interaction, enabled=None, channel=None)

    args, _ = interaction.response.send_message.await_args
    assert "off" in args[0]


async def test_monthly_wrapup_enable_stores_under_monthly_kind(fake_bot):
    cog = alerts_cog.Alerts(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.channel = _fake_channel(cid=321)

    await cog.monthly_wrapup.callback(cog, interaction, enabled=True, channel=None)

    assert config_store.get_guild_alert(999, "monthly")["channel_id"] == 321
    # Weekly is untouched.
    assert config_store.get_guild_alert(999, "weekly")["enabled"] is False


async def test_wrapup_commands_require_manage_guild_permission():
    interaction = make_interaction(guild_id=999)
    interaction.permissions = discord.Permissions.none()

    [weekly_predicate] = alerts_cog.Alerts.weekly_wrapup.checks
    [monthly_predicate] = alerts_cog.Alerts.monthly_wrapup.checks
    with pytest.raises(MissingPermissions):
        weekly_predicate(interaction)
    with pytest.raises(MissingPermissions):
        monthly_predicate(interaction)


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
