"""Tests for TadokuBot.on_application_command_error.

Verifies the error-type-to-message mapping (missing permission, cooldown,
generic fallback), that it uses followup when the response is already done, and
that a failure to even deliver the error message is swallowed rather than
propagating.
"""

from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from discord.app_commands.checks import Cooldown

from bot import TadokuBot
from tests.conftest import make_interaction


@pytest.fixture
def tadoku_bot():
    return TadokuBot()


async def test_missing_permissions_gets_friendly_message(tadoku_bot):
    interaction = make_interaction(guild_id=999)
    error = app_commands.MissingPermissions(["manage_guild"])

    await tadoku_bot.on_application_command_error(interaction, error)

    args, kwargs = interaction.response.send_message.await_args
    assert "Manage Server" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_command_on_cooldown_reports_retry_after(tadoku_bot):
    interaction = make_interaction(guild_id=999)
    error = app_commands.CommandOnCooldown(Cooldown(1, 30), retry_after=12.7)

    await tadoku_bot.on_application_command_error(interaction, error)

    args, kwargs = interaction.response.send_message.await_args
    assert "12s" in args[0]


async def test_unexpected_error_gets_generic_message(tadoku_bot):
    interaction = make_interaction(guild_id=999)
    error = app_commands.AppCommandError("something broke")

    await tadoku_bot.on_application_command_error(interaction, error)

    args, kwargs = interaction.response.send_message.await_args
    assert "Something went wrong" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_uses_followup_when_response_already_done(tadoku_bot):
    interaction = make_interaction(guild_id=999)
    interaction.response.is_done = lambda: True
    error = app_commands.AppCommandError("something broke")

    await tadoku_bot.on_application_command_error(interaction, error)

    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_awaited_once()


async def test_delivery_failure_does_not_raise(tadoku_bot):
    interaction = make_interaction(guild_id=999)
    interaction.response.send_message.side_effect = discord.HTTPException(
        response=SimpleNamespace(status=500, reason="Internal Server Error"), message="nope"
    )
    error = app_commands.AppCommandError("something broke")

    # Should be swallowed and logged, not propagate.
    await tadoku_bot.on_application_command_error(interaction, error)
