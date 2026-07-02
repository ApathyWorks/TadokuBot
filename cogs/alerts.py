"""Alerts cog: automatic weekly/monthly wrap-up posts, plus their admin toggles.

Two slash commands let a server manager turn each wrap-up on or off and choose
where it posts:

  * ``/weekly_wrapup``  -- post the last-7-days ranking at the start of each week.
  * ``/monthly_wrapup`` -- post the just-ended month's ranking on the 1st.

Both require the **Manage Server** permission. A background loop (``check_alerts``)
runs hourly: for every guild with an alert enabled, once the calendar period
rolls over it renders the same embed the ``/weeklyleaderboard`` /
``/monthlyleaderboard`` commands produce (via
``cogs.leaderboard.build_period_leaderboard_embed``) and posts it to the
configured channel. A per-guild ``last_period`` marker makes each wrap-up fire
exactly once per period and survive bot restarts (it catches up on the next tick
if the bot was down when the period rolled over).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import cogs.leaderboard as leaderboard
import lib.config_store as config_store
import lib.tadoku_client as tadoku

_log = logging.getLogger(__name__)

# How often the scheduler wakes to check whether a period has rolled over. Hourly
# is plenty: a wrap-up only needs to land within an hour of the week/month
# boundary, and the last_period marker guarantees it posts just once.
CHECK_INTERVAL_HOURS = 1


def _period_key(kind: str, now: datetime) -> list[int]:
    """Identify the calendar period ``now`` falls in for the given alert kind.

    Weekly uses the ISO year+week (so it advances every Monday); monthly uses
    year+month. Returned as a plain list so it round-trips through JSON and
    compares by value against the stored ``last_period``.
    """
    if kind == "weekly":
        iso = now.isocalendar()
        return [iso[0], iso[1]]
    return [now.year, now.month]


def _window_for(kind: str, now: datetime) -> tuple[datetime, Optional[datetime], str, str]:
    """Return ``(cutoff, until, title_suffix, window_phrase)`` for a wrap-up.

    Weekly mirrors ``/weeklyleaderboard``: the rolling last 7 days ending now,
    open-ended at the top. Monthly summarises the *previous* calendar month --
    the one that just ended -- since the alert fires on the 1st of the new month.
    """
    if kind == "weekly":
        cutoff = now - timedelta(days=leaderboard.WEEKLY_WINDOW_DAYS)
        return (
            cutoff,
            None,
            f"last {leaderboard.WEEKLY_WINDOW_DAYS} days",
            f"the last {leaderboard.WEEKLY_WINDOW_DAYS} days",
        )
    # Monthly: the month before the one ``now`` sits in.
    prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    cutoff = datetime(prev_year, prev_month, 1, tzinfo=timezone.utc)
    until = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    label = cutoff.strftime("%B %Y")
    return cutoff, until, label, label


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Start the background scheduler when the cog is added to the bot.

        Done here (not in ``__init__``) so simply constructing the cog -- as the
        tests do -- doesn't spin up a live loop; only a real ``add_cog`` does.
        """
        self.check_alerts.start()

    async def cog_unload(self) -> None:
        """Stop the scheduler when the cog is unloaded/bot shuts down."""
        self.check_alerts.cancel()

    # -- scheduler ----------------------------------------------------------

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def check_alerts(self) -> None:
        """Hourly tick: post any wrap-ups whose period has rolled over."""
        await self._run_due_alerts(datetime.now(timezone.utc))

    @check_alerts.before_loop
    async def _before_check_alerts(self) -> None:
        # Don't touch channels until the gateway connection is up.
        await self.bot.wait_until_ready()

    async def _run_due_alerts(self, now: datetime) -> None:
        """Post every enabled wrap-up that hasn't yet fired for its current period.

        Pulled out of the loop body so tests can drive it directly with a fixed
        ``now``. Each guild/kind is independent; one failing never blocks the
        others.
        """
        for guild_id in config_store.guilds_with_alerts():
            for kind in config_store.ALERT_KINDS:
                try:
                    await self._maybe_post(guild_id, kind, now)
                except Exception:  # noqa: BLE001 -- one bad guild mustn't stop the rest
                    _log.exception("Wrap-up (%s) failed for guild %s", kind, guild_id)

    async def _maybe_post(self, guild_id: int, kind: str, now: datetime) -> None:
        """Post the ``kind`` wrap-up for one guild if it's due, then mark it done.

        "Due" means the alert is enabled and the current calendar period differs
        from the last one posted. On a tadoku.app failure we leave ``last_period``
        untouched so the next tick retries; otherwise (posted, or nothing to
        post) we advance it so the wrap-up fires exactly once per period.
        """
        settings = config_store.get_guild_alert(guild_id, kind)
        if not settings["enabled"]:
            return

        period = _period_key(kind, now)
        if settings["last_period"] == period:
            return  # already handled this week/month

        cutoff, until, title_suffix, window_phrase = _window_for(kind, now)
        try:
            _contest, embed = await leaderboard.build_period_leaderboard_embed(
                self.bot,
                guild_id,
                cutoff=cutoff,
                until=until,
                title_suffix=title_suffix,
                window_phrase=window_phrase,
            )
        except tadoku.TadokuAPIError:
            # Transient (or contest gone): don't advance last_period, so we retry.
            _log.warning(
                "Wrap-up (%s) for guild %s: tadoku.app lookup failed; will retry", kind, guild_id
            )
            return

        if embed is not None:
            await self._post(guild_id, settings["channel_id"], embed)
        # Advance the marker whether we posted or there was simply nothing to
        # post, so an empty period doesn't get re-checked every hour.
        config_store.set_guild_alert(guild_id, kind, last_period=period)

    async def _post(self, guild_id: int, channel_id: Optional[int], embed: discord.Embed) -> None:
        """Send ``embed`` to the configured channel, tolerating a missing channel.

        A channel that's been deleted or that the bot can no longer see/post to
        is logged and skipped rather than raised -- the wrap-up for that period
        is simply lost (the caller has already advanced ``last_period``).
        """
        if channel_id is None:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                _log.warning("Wrap-up for guild %s: channel %s not found", guild_id, channel_id)
                return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            _log.warning(
                "Wrap-up for guild %s: couldn't post to channel %s", guild_id, channel_id
            )

    # -- admin commands -----------------------------------------------------

    async def _configure_alert(
        self,
        interaction: discord.Interaction,
        kind: str,
        enabled: Optional[bool],
        channel: Optional[discord.TextChannel],
        label: str,
        cadence: str,
    ) -> None:
        """Shared handler for /weekly_wrapup and /monthly_wrapup.

        With ``enabled`` omitted it reports the current setting; ``False`` turns
        the alert off; ``True`` turns it on, posting in ``channel`` (defaulting to
        the channel the command was run in). On enable we seed ``last_period`` to
        the current period so the first post lands at the *next* boundary rather
        than immediately. All replies are ephemeral.
        """
        settings = config_store.get_guild_alert(interaction.guild_id, kind)

        if enabled is None:
            if settings["enabled"] and settings["channel_id"]:
                await interaction.response.send_message(
                    f"The {label} is **on**, posting in <#{settings['channel_id']}> {cadence}.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"The {label} is **off**. Use `enabled:True` to turn it on.",
                    ephemeral=True,
                )
            return

        if not enabled:
            config_store.set_guild_alert(interaction.guild_id, kind, enabled=False)
            await interaction.response.send_message(
                f"✅ The {label} is now **off**.", ephemeral=True
            )
            return

        # Enabling: post in the given channel, or the one the command was used in.
        target = channel or interaction.channel
        config_store.set_guild_alert(
            interaction.guild_id,
            kind,
            enabled=True,
            channel_id=target.id,
            # Seed to the current period so we don't fire an extra post right now.
            last_period=_period_key(kind, datetime.now(timezone.utc)),
        )
        await interaction.response.send_message(
            f"✅ The {label} is now **on**, posting in {target.mention} {cadence}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="weekly_wrapup",
        description="Turn the automatic weekly leaderboard wrap-up on or off for this server.",
    )
    @app_commands.describe(
        enabled="Turn the weekly wrap-up on or off. Omit to see the current setting.",
        channel="Channel to post the wrap-up in (defaults to this channel).",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def weekly_wrapup(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Configure the weekly wrap-up (see ``_configure_alert``)."""
        await self._configure_alert(
            interaction, "weekly", enabled, channel, "weekly wrap-up", "at the start of each week"
        )

    @app_commands.command(
        name="monthly_wrapup",
        description="Turn the automatic monthly leaderboard wrap-up on or off for this server.",
    )
    @app_commands.describe(
        enabled="Turn the monthly wrap-up on or off. Omit to see the current setting.",
        channel="Channel to post the wrap-up in (defaults to this channel).",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def monthly_wrapup(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Configure the monthly wrap-up (see ``_configure_alert``)."""
        await self._configure_alert(
            interaction, "monthly", enabled, channel, "monthly wrap-up", "on the 1st of each month"
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py extension entry point; called by ``load_extension``."""
    await bot.add_cog(Alerts(bot))
