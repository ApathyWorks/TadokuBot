# TadokuBot

A Discord bot that shows [tadoku.app](https://tadoku.app) contest leaderboards. Read-only: all
scoring data comes live from tadoku.app's public API, the bot doesn't keep its own copy of any
logs or scores.

## Commands

Run `/tadokubot` in Discord for this same list in-app. When the bot (re)starts, it also posts a
one-time "I'm online — run `/tadokubot`" pointer to each server's log-feed channel (or, failing
that, its alerts channel) — if neither is set, it stays quiet.

### General commands

Anyone can use these.

| Command | Description |
| --- | --- |
| `/tadokubot` | Lists all of the bot's commands (this table). |
| `/leaderboard [page] [language] [activity]` | Shows this server's configured contest leaderboard. Falls back to the latest official tadoku.app contest if nothing is configured. |
| `/score username:<name>` | Looks up one person's rank and score in this server's current contest. `username` is their Tadoku display name; the person must be on that leaderboard (otherwise the bot says they aren't participating). |
| `/weeklyleaderboard` | Ranks everyone by points logged in the **last 7 days** of this server's current contest. Tallied from the contest's individual logs (the API's own leaderboard is cumulative), so it's a rolling window ending now. When the shame setting is on (default), it also appends a call-out of everyone who has points in the contest but logged nothing in the last 7 days. |
| `/monthlyleaderboard [month] [year]` | Like `/weeklyleaderboard`, but ranks points logged in a **calendar month**. With no arguments it shows the current month to date; pass `month` and/or `year` (e.g. `month:June year:2026`) to see a specific past month. Each defaults to the current one. Uses the same shame setting and call-out. |
| `/current_contest` | Shows which contest this server is currently configured to display. |
| `/claim username:<name>` | Links your Discord account to your tadoku.app username (the person must be on the current contest's leaderboard). One claim per member, and each username can be claimed only once. See [Discord ↔ Tadoku matching](#discord--tadoku-matching). |
| `/unclaim` | Removes the username you claimed, freeing it for anyone else. |
| `/unclaimedlist` | Lists the current contest's participants that nobody has claimed yet. |

### Admin commands (Manage Server)

These require the **Manage Server** permission — or membership in a role named in the optional
`ADMIN_ROLES` env var (see [Setup](#setup)). They're visible to everyone in the slash menu but
reject unauthorized users with an ephemeral message.

| Command | Description |
| --- | --- |
| `/set_contest contest:<search>` | Picks which contest `/leaderboard` shows for this server, with autocomplete search over tadoku.app's contest list. |
| `/shame [enabled]` | Turns the shame call-out on `/weeklyleaderboard` and `/monthlyleaderboard` on or off for this server (default **on**). Run without `enabled` to see the current setting. |
| `/alerts on [channel]` / `/alerts off` / `/alerts status` | One switch for all automatic leaderboard posts. See [Scheduled alerts](#scheduled-alerts). |
| `/log on [channel]` / `/log off` / `/log status` | Live feed of new contest logs to a channel. See [Live log feed](#live-log-feed). |
| `/autoclaim` | Bulk-links every current-contest participant to the Discord member of the same name. See [Discord ↔ Tadoku matching](#discord--tadoku-matching). |

## Scheduled alerts

`/alerts on channel:#somewhere` opts a server into three automatic posts (all times **UTC**), each
for that server's current contest — one on/off switch, one channel (defaults to the channel you run
`/alerts on` in):

| Alert | When | Content |
| --- | --- | --- |
| Weekly | Start of each week (**Monday 00:00**) | The rolling last-7-days ranking (same as `/weeklyleaderboard`). |
| Monthly | The **1st, 00:00** | The just-ended month's ranking (same as `/monthlyleaderboard` for that month). |
| Year-end | **Jan 1, 00:00** | The contest's final cumulative standings (same as `/leaderboard`), topped with a **top-3 podium congratulation**. |

`/alerts off` disables all three; `/alerts status` shows the current channel. A background task
checks hourly and posts each alert at most once per period, so it fires correctly across restarts or
a missed midnight. The bot must be able to post in the chosen channel — `/alerts on` refuses one it
can't send to.

## Live log feed

`/log on channel:#somewhere` turns on a live feed of the server's current contest: every **1
minute** the bot checks tadoku.app for new logs and posts each one — who logged it, what they
logged (activity, amount, title, language), and the points — to the channel as an embed **card**,
one per log, colour-coded by activity.

If the logger has linked their Discord account with [`/claim`](#discord--tadoku-matching), the log
instead posts as a rendered **profile card image** (dark-themed): their Discord avatar, their
**immersion stats since the start of 2026** (characters, pages, listening hours), and the log they
just made. Those totals are summed live from tadoku.app's per-user log history (from 2026 onward,
across every contest), so they reflect the member's real numbers — no local tally is kept. Unlinked
loggers get the plain embed card.

The card is drawn with Pillow, including the material title (often Japanese) — the Docker image
installs `fonts-dejavu-core` and `fonts-noto-cjk` so both Latin and CJK render (a built-in font is
the fallback). The bot needs **Embed Links** permission for the plain embed cards and **Attach
Files** for the rendered profile-card images (in addition to View Channel / Send Messages).

Only logs made *after* you run `/log on` are posted (no backlog dump); a per-server high-water mark
keeps it from repeating. A burst is capped per poll with an "…and N more" note. `/log off` stops it;
`/log status` shows the channel. Defaults to the channel you run `/log on` in, and the bot must be
able to post there.

## Discord ↔ Tadoku matching

The bot can remember which Discord member is which tadoku.app participant, per server. The mapping
is two-way unique: **each member claims at most one username, and each username is claimed by at most
one member** (matching is case- and whitespace-insensitive, like `/score`).

- **`/autoclaim`** (admin) does the bulk work: for every participant in the current contest it looks
  up a Discord member of the same name (username, nickname, or global name) and links them. It only
  pairs an *unambiguous* match — a name that matches zero or multiple members is skipped rather than
  guessed — and never overwrites an existing claim. The member lookup uses Discord's name search, so
  no privileged intents are required.
- **`/claim username`** is the manual fallback for anyone `/autoclaim` couldn't match (e.g. their
  Discord name differs from their Tadoku name). It refuses a username that isn't on the leaderboard,
  one already claimed by someone else, or a second claim by a member who already has one.
- **`/unclaim`** frees your username; **`/unclaimedlist`** shows who's still unmatched.

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
   Optionally, let extra roles run the admin commands (in addition to Manage Server) by naming them
   (comma-separated, case-insensitive) — changing this needs a restart:
   ```
   ADMIN_ROLES=Moderator,Officers
   ```
   Optionally, log in to tadoku.app so API calls carry an authenticated session (needed if the
   endpoints you use are gated/rate-limited). tadoku uses Ory Kratos, whose `ory_kratos_session`
   cookie expires — the bot logs in with these credentials on startup and, when a request comes back
   401/403, re-logs-in and retries automatically. Use a **dedicated bot account without 2FA** (2FA
   blocks automated login). Leave blank to use the public endpoints anonymously (the default).
   ```
   TADOKU_EMAIL=bot@example.com
   TADOKU_PASSWORD=your_password
   # KRATOS_PUBLIC_URL defaults to https://account.tadoku.app/kratos
   ```
4. Run it:
   ```
   python main.py
   ```

Slash commands sync globally on startup — it can take a short while for Discord to propagate
new/changed commands to clients.

## Local state

The only thing persisted locally is `data/config.json`, a per-server mapping of settings: which
contest `/leaderboard` should display, whether the shame call-out is on, each server's alert
settings (enabled, target channel, and the last period posted for each of the weekly/monthly/yearly
alerts), the log-feed settings, and the Discord-member ↔ Tadoku-username claims. Everything else
(contest details, scores, rankings) is fetched live from
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
