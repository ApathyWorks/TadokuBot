"""Tests for the help cog (/tadokubot + the online announcement).

Covers the command list embed, a drift guard that the catalogue stays in sync
with the real command tree, the startup-channel resolution, and the once-per-
process announcement (with a fake guild/channel).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands, tasks

import cogs.help_command as help_cog
import lib.config_store as config_store
from tests.conftest import make_interaction


# ---------------------------------------------------------------------------
# /tadokubot embed + catalogue
# ---------------------------------------------------------------------------

async def test_tadokubot_replies_with_grouped_ephemeral_embed(fake_bot):
    cog = help_cog.Help(fake_bot)
    interaction = make_interaction(guild_id=999)

    await help_cog.Help.tadokubot.callback(cog, interaction)

    args, kwargs = interaction.response.send_message.await_args
    assert kwargs.get("ephemeral") is True
    embed = kwargs["embed"]
    field_names = [f.name for f in embed.fields]
    assert "Commands" in field_names
    assert "Admin (Manage Server)" in field_names
    # A representative command from each group is listed.
    body = "\n".join(f.value for f in embed.fields)
    assert "/leaderboard" in body and "/set_contest" in body
    # The GitHub repo is linked (title url + a Source field).
    assert embed.url == help_cog.REPO_URL
    assert help_cog.REPO_URL in body


def test_admin_commands_are_the_manage_server_ones():
    admin_names = {n.lstrip("/").split()[0] for n, _ in help_cog.ADMIN_COMMANDS}
    assert admin_names == {"set_contest", "shame", "alerts", "log", "autoclaim"}


async def test_help_catalogue_covers_every_registered_command(monkeypatch):
    # Guard against a command being added without being listed in /tadokubot.
    # No-op the scheduler loops so loading the alerts/log_feed cogs here doesn't
    # spin up a live tasks.loop against a never-connected bot.
    monkeypatch.setattr(tasks.Loop, "start", lambda self, *a, **k: None)
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    for ext in (
        "cogs.leaderboard", "cogs.admin", "cogs.alerts", "cogs.log_feed",
        "cogs.claims", "cogs.help_command",
    ):
        await bot.load_extension(ext)

    tree_names = {c.name for c in bot.tree.get_commands()}
    listed = {
        n.lstrip("/").split()[0]
        for n, _ in help_cog.GENERAL_COMMANDS + help_cog.ADMIN_COMMANDS
    }
    assert tree_names == listed


# ---------------------------------------------------------------------------
# _startup_channel_id
# ---------------------------------------------------------------------------

def test_startup_channel_prefers_enabled_logfeed():
    config_store.set_guild_logfeed(1, enabled=True, channel_id=111)
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=222)

    assert help_cog._startup_channel_id(1) == 111


def test_startup_channel_falls_back_to_alerts():
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=222)

    assert help_cog._startup_channel_id(1) == 222


def test_startup_channel_ignores_disabled_feeds():
    config_store.set_guild_logfeed(1, enabled=False, channel_id=111)
    config_store.set_guild_alert(1, "weekly", enabled=False, channel_id=222)

    assert help_cog._startup_channel_id(1) is None


def test_startup_channel_none_when_nothing_configured():
    assert help_cog._startup_channel_id(1) is None


# ---------------------------------------------------------------------------
# online announcement (on_ready)
# ---------------------------------------------------------------------------

def _guild(guild_id=1, channel=None):
    me = object()
    guild = SimpleNamespace(id=guild_id, me=me)
    guild.get_channel = lambda cid, ch=channel: ch if (ch and cid == ch.id) else None
    return guild


def _channel(cid=111, can_send=True):
    perms = SimpleNamespace(send_messages=can_send)
    return SimpleNamespace(id=cid, send=AsyncMock(), permissions_for=lambda m: perms)


async def test_announce_posts_to_the_configured_channel():
    config_store.set_guild_logfeed(1, enabled=True, channel_id=111)
    channel = _channel(cid=111)
    bot = SimpleNamespace(guilds=[_guild(1, channel)], get_channel=lambda cid: None)
    cog = help_cog.Help(bot)

    await cog.on_ready()

    channel.send.assert_awaited_once()
    assert "/tadokubot" in channel.send.await_args.args[0]


async def test_announce_runs_once_per_process():
    config_store.set_guild_logfeed(1, enabled=True, channel_id=111)
    channel = _channel(cid=111)
    bot = SimpleNamespace(guilds=[_guild(1, channel)], get_channel=lambda cid: None)
    cog = help_cog.Help(bot)

    await cog.on_ready()
    await cog.on_ready()  # a reconnect -- must not repost

    channel.send.assert_awaited_once()


async def test_announce_skips_guild_with_no_bot_channel():
    channel = _channel(cid=111)
    bot = SimpleNamespace(guilds=[_guild(1, channel)], get_channel=lambda cid: None)
    cog = help_cog.Help(bot)

    await cog.on_ready()  # nothing configured for guild 1

    channel.send.assert_not_awaited()


async def test_announce_skips_channel_it_cannot_post_to():
    config_store.set_guild_logfeed(1, enabled=True, channel_id=111)
    channel = _channel(cid=111, can_send=False)
    bot = SimpleNamespace(guilds=[_guild(1, channel)], get_channel=lambda cid: None)
    cog = help_cog.Help(bot)

    await cog.on_ready()

    channel.send.assert_not_awaited()
