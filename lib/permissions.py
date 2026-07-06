"""Who may use the admin commands.

Admin commands are open to anyone with Discord's **Manage Server** permission
*or* a member of a role named in the ``ADMIN_ROLES`` env var (matched
case-insensitively). The bot stores the configured names (casefolded) on
``bot.admin_roles``; the ``is_admin()`` check reads them via
``interaction.client.admin_roles``.
"""

import discord
from discord import app_commands


class NotAdmin(app_commands.CheckFailure):
    """Raised when a caller has neither Manage Server nor an authorized role."""


def parse_admin_roles(raw: str | None) -> set[str]:
    """Parse the ``ADMIN_ROLES`` env value into a set of casefolded role names.

    Comma-separated; surrounding whitespace is trimmed, empties dropped, and
    names casefolded so matching is case-insensitive. ``None``/empty -> ``set()``.
    """
    if not raw:
        return set()
    return {name.strip().casefold() for name in raw.split(",") if name.strip()}


def user_is_admin(interaction: discord.Interaction) -> bool:
    """Whether ``interaction``'s caller may use the admin commands.

    True if their resolved permissions include Manage Server, or they hold a
    role whose (casefolded) name is in the bot's ``admin_roles`` allowlist.
    """
    # Manage Server always qualifies -- short-circuit before touching roles.
    perms = getattr(interaction, "permissions", None)
    if perms is not None and perms.manage_guild:
        return True

    admin_roles = getattr(interaction.client, "admin_roles", None) or set()
    if not admin_roles:
        return False
    # interaction.user is a Member in a guild (roles only exist in guilds).
    roles = getattr(interaction.user, "roles", None) or []
    return any(role.name.casefold() in admin_roles for role in roles)


def is_admin():
    """An ``app_commands.check`` that allows Manage Server or an authorized role."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if user_is_admin(interaction):
            return True
        raise NotAdmin()

    return app_commands.check(predicate)
