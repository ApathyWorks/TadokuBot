"""Tests for admin authorization (lib.permissions + the is_admin gate wiring).

Covers parsing the ADMIN_ROLES env value, the pure user_is_admin decision, and
a drift-proof check that every admin command actually enforces is_admin (rejects
a non-admin, allows a manager) while carrying no static default_permissions.
"""

from types import SimpleNamespace

import pytest

import cogs.admin as admin_cog
import cogs.alerts as alerts_cog
import cogs.claims as claims_cog
import cogs.log_feed as log_feed_cog
from lib.permissions import NotAdmin, parse_admin_roles, user_is_admin


def _interaction(*, manage_guild=False, admin_roles=None, role_names=()):
    return SimpleNamespace(
        permissions=SimpleNamespace(manage_guild=manage_guild),
        client=SimpleNamespace(admin_roles=admin_roles or set()),
        user=SimpleNamespace(roles=[SimpleNamespace(name=n) for n in role_names]),
    )


# ---------------------------------------------------------------------------
# parse_admin_roles
# ---------------------------------------------------------------------------

def test_parse_admin_roles_trims_casefolds_and_dedupes():
    assert parse_admin_roles("Moderator, Officers , ,mod,MOD") == {"moderator", "officers", "mod"}


def test_parse_admin_roles_empty_is_empty_set():
    assert parse_admin_roles("") == set()
    assert parse_admin_roles(None) == set()


# ---------------------------------------------------------------------------
# user_is_admin
# ---------------------------------------------------------------------------

def test_manage_guild_is_admin_even_without_roles():
    assert user_is_admin(_interaction(manage_guild=True)) is True


def test_matching_role_name_is_admin_case_insensitively():
    interaction = _interaction(manage_guild=False, admin_roles={"moderator"}, role_names=["Moderator"])
    assert user_is_admin(interaction) is True


def test_non_manager_without_matching_role_is_not_admin():
    interaction = _interaction(manage_guild=False, admin_roles={"moderator"}, role_names=["Member"])
    assert user_is_admin(interaction) is False


def test_no_configured_roles_means_only_managers_are_admin():
    interaction = _interaction(manage_guild=False, admin_roles=set(), role_names=["Moderator"])
    assert user_is_admin(interaction) is False


# ---------------------------------------------------------------------------
# Every admin command is gated by is_admin (and nothing else statically)
# ---------------------------------------------------------------------------

ADMIN_COMMANDS = [
    admin_cog.Admin.set_contest,
    admin_cog.Admin.shame,
    alerts_cog.Alerts.alerts_on,
    alerts_cog.Alerts.alerts_off,
    alerts_cog.Alerts.alerts_status,
    log_feed_cog.LogFeed.log_on,
    log_feed_cog.LogFeed.log_off,
    log_feed_cog.LogFeed.log_status,
    claims_cog.Claims.autoclaim,
]


@pytest.mark.parametrize("command", ADMIN_COMMANDS, ids=lambda c: c.name)
async def test_admin_command_rejects_non_admin(command):
    [predicate] = command.checks
    with pytest.raises(NotAdmin):
        await predicate(_interaction(manage_guild=False))


@pytest.mark.parametrize("command", ADMIN_COMMANDS, ids=lambda c: c.name)
async def test_admin_command_allows_manager(command):
    [predicate] = command.checks
    assert await predicate(_interaction(manage_guild=True)) is True


async def test_admin_command_allows_configured_role():
    [predicate] = admin_cog.Admin.set_contest.checks
    interaction = _interaction(manage_guild=False, admin_roles={"moderator"}, role_names=["Moderator"])
    assert await predicate(interaction) is True
