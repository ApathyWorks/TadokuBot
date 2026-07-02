# TadokuBot

A Discord bot that shows [tadoku.app](https://tadoku.app) contest leaderboards. Read-only: all
scoring data comes live from tadoku.app's public API, the bot doesn't keep its own copy of any
logs or scores.

## Commands

| Command | Description |
| --- | --- |
| `/leaderboard [page] [language] [activity]` | Shows this server's configured contest leaderboard. Falls back to the latest official tadoku.app contest if nothing is configured. |
| `/score username:<name>` | Looks up one person's rank and score in this server's current contest. `username` is their Tadoku display name; the person must be on that leaderboard (otherwise the bot says they aren't participating). |
| `/weeklyleaderboard` | Ranks everyone by points logged in the **last 7 days** of this server's current contest. Tallied from the contest's individual logs (the API's own leaderboard is cumulative), so it's a rolling window ending now. When the shame setting is on (default), it also appends a call-out of everyone who has points in the contest but logged nothing in the last 7 days. |
| `/monthlyleaderboard [month] [year]` | Like `/weeklyleaderboard`, but ranks points logged in a **calendar month**. With no arguments it shows the current month to date; pass `month` and/or `year` (e.g. `month:June year:2026`) to see a specific past month. Each defaults to the current one. Uses the same shame setting and call-out. |
| `/set_contest contest:<search>` | **Manage Server** permission required. Picks which contest `/leaderboard` shows for this server, with autocomplete search over tadoku.app's contest list. |
| `/current_contest` | Shows which contest this server is currently configured to display. |
| `/shame [enabled]` | **Manage Server** permission required. Turns the shame call-out on `/weeklyleaderboard` and `/monthlyleaderboard` on or off for this server (default **on**). Run without `enabled` to see the current setting. |
| `/weekly_wrapup [enabled] [channel]` | **Manage Server** permission required. Turns the automatic **weekly wrap-up** on or off. When on, the bot posts the weekly leaderboard to the chosen channel (or the channel the command was run in) at the start of each week. Run without `enabled` to see the current setting. |
| `/monthly_wrapup [enabled] [channel]` | **Manage Server** permission required. Turns the automatic **monthly wrap-up** on or off. When on, the bot posts the previous month's leaderboard to the chosen channel on the 1st of each month. Run without `enabled` to see the current setting. |

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Create a Discord bot application in the [Discord Developer Portal](https://discord.com/developers/applications), copy its token, and under OAuth2 > URL Generator enable the `bot` and `applications.commands` scopes to generate an invite link. No privileged intents are needed.
3. Copy `.env.example` to `.env` and paste in the token:
   ```
   DISCORD_TOKEN=your_token_here
   ```
4. Run it:
   ```
   python main.py
   ```

Slash commands sync globally on startup — it can take a short while for Discord to propagate
new/changed commands to clients.

## Local state

The only thing persisted locally is `data/config.json`, a per-server mapping of settings: which
contest `/leaderboard` should display, whether the shame call-out is on, and each server's
weekly/monthly wrap-up alert settings (enabled, target channel, and the last period posted).
Everything else (contest details, scores, rankings) is fetched live from
`https://tadoku.app/api/internal/immersion/`.

## Running tests

```
pip install -r requirements-dev.txt
pytest
```

The suite (`tests/`) covers `lib/tadoku_client.py` against a real local `aiohttp` test server
(no network calls, no mocking-library version drift), `lib/config_store.py`'s persistence logic,
and every cog's command/autocomplete logic by invoking their callbacks directly with a fake
`discord.Interaction` — no live Discord connection needed. The wrap-up scheduler is driven
directly with a fixed "now" (no reliance on the wall clock or a live loop). New features should
come with tests covering their behavior in the same style.
