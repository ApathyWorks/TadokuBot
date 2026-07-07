"""Per-guild "which contest is displayed" setting, persisted as JSON.

This is the *only* local state the bot keeps -- everything else (contest
details, scores, rankings) is fetched live from tadoku.app on demand. The
store is a single JSON object keyed by Discord guild id (as a string, since
JSON object keys are always strings):

    {
      "123456789": {
        "contest_id": "<uuid>",
        "contest_title": "2026 Round 4",
        "shame": true
      }
    }

Each guild's keys are independent: pinning a contest and toggling the
``/weeklyleaderboard`` shame list are separate settings that don't clobber
each other. ``shame`` is absent until a guild toggles it (the feature is on by
default).

Writes are done to a temp file and atomically renamed into place, so a crash
partway through a write can never leave a half-written / corrupt config.json.
"""

import json
import os

# Absolute path to data/config.json, resolved relative to this file so the
# store works regardless of the process's current working directory.
# __file__ -> lib/config_store.py, so two dirnames up is the project root.
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")


def _read() -> dict:
    """Load the whole config object, or an empty dict if it doesn't exist yet.

    A missing file is the normal first-run state, not an error, so it maps to
    an empty mapping rather than raising.
    """
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r") as f:
        return json.load(f)


def _write(data: dict) -> None:
    """Persist the whole config object atomically.

    Writing to a sibling ``.tmp`` file and then ``os.replace``-ing it over the
    real path makes the swap atomic on POSIX: readers see either the old
    complete file or the new complete file, never a partial write.
    """
    tmp_path = f"{_PATH}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _PATH)


def get_guild_contest(guild_id: int) -> dict | None:
    """Return the contest configured for ``guild_id``, or ``None`` if unset.

    The returned dict has ``contest_id`` and ``contest_title`` keys. ``None``
    means the server has never run ``/set_contest`` and should fall back to the
    latest official contest.
    """
    # JSON keys are strings, so look up by the stringified guild id.
    entry = _read().get(str(guild_id))
    # A guild may have an entry with only other settings (e.g. the shame
    # toggle) and no contest pinned -- that still counts as "unset" here, so
    # callers fall back to the latest-official contest rather than crashing on a
    # missing contest_id.
    if not entry or "contest_id" not in entry:
        return None
    return entry


def set_guild_contest(guild_id: int, contest_id: str, contest_title: str) -> None:
    """Pin ``guild_id`` to a specific contest, overwriting any previous choice.

    We store the title alongside the id so ``/current_contest`` can name the
    contest without an extra API round-trip.
    """
    # Read-modify-write the whole object: load current state, update just this
    # guild's contest keys (preserving any others, e.g. the shame toggle), and
    # write it all back atomically.
    data = _read()
    entry = data.get(str(guild_id), {})
    entry["contest_id"] = contest_id
    entry["contest_title"] = contest_title
    data[str(guild_id)] = entry
    _write(data)


def get_guild_shame(guild_id: int) -> bool:
    """Whether ``/weeklyleaderboard`` appends its "shame" list for this guild.

    Defaults to ``True`` (on) for any guild that has never toggled it, matching
    the feature's default-on behaviour. A ``None`` guild id (a DM) has no stored
    setting either, so it also gets the default.
    """
    entry = _read().get(str(guild_id))
    if not entry:
        return True
    # Older entries written before this setting existed have no "shame" key;
    # treat their absence as the on-by-default state.
    return entry.get("shame", True)


def set_guild_shame(guild_id: int, enabled: bool) -> None:
    """Turn the ``/weeklyleaderboard`` shame list on/off for ``guild_id``.

    Stored alongside (not replacing) any pinned contest, so toggling shame never
    clears ``/set_contest`` and vice versa.
    """
    # Read-modify-write, preserving the guild's other keys (e.g. contest pin).
    data = _read()
    entry = data.get(str(guild_id), {})
    entry["shame"] = enabled
    data[str(guild_id)] = entry
    _write(data)


# The automatic alerts, each configured independently per guild. All three share
# one on/off switch and channel via ``/alerts``, but keep their own ``last_period``
# marker so they fire on their own boundaries (Monday / the 1st / Jan 1).
ALERT_KINDS = ("weekly", "monthly", "yearly")


def get_guild_alert(guild_id: int, kind: str) -> dict:
    """Return a guild's settings for the ``kind`` ("weekly"/"monthly"/"yearly") alert.

    Always returns a dict with all three keys so callers never have to guard for
    absence:

      * ``enabled``     -- whether the alert posts automatically (default False).
      * ``channel_id``  -- the channel to post in (int), or ``None`` if unset.
      * ``last_period`` -- the last period already posted, as ``[year, week]``
        (weekly), ``[year, month]`` (monthly) or ``[year]`` (yearly), or ``None``
        if never posted. The scheduler uses this to fire once per period and to
        avoid re-posting.
    """
    if kind not in ALERT_KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    entry = _read().get(str(guild_id)) or {}
    settings = (entry.get("alerts") or {}).get(kind) or {}
    return {
        "enabled": settings.get("enabled", False),
        "channel_id": settings.get("channel_id"),
        "last_period": settings.get("last_period"),
    }


def set_guild_alert(guild_id: int, kind: str, **fields) -> None:
    """Update some of a guild's ``kind`` wrap-up settings, leaving the rest as-is.

    Accepts any of ``enabled`` / ``channel_id`` / ``last_period`` as keyword
    args and merges them into the stored settings, preserving the guild's other
    keys (contest pin, shame toggle, the other alert kind). This partial-update
    shape lets the scheduler bump only ``last_period`` without disturbing the
    admin's ``enabled``/``channel_id`` choices.
    """
    if kind not in ALERT_KINDS:
        raise ValueError(f"unknown alert kind: {kind!r}")
    unknown = set(fields) - {"enabled", "channel_id", "last_period"}
    if unknown:
        raise ValueError(f"unknown alert field(s): {sorted(unknown)}")
    data = _read()
    entry = data.get(str(guild_id), {})
    alerts = entry.setdefault("alerts", {})
    alerts.setdefault(kind, {}).update(fields)
    data[str(guild_id)] = entry
    _write(data)


def guilds_with_alerts() -> list[int]:
    """Return the ids of guilds that have any wrap-up alert configured.

    Lets the scheduler iterate just the guilds it might need to post for,
    without loading or reasoning about every stored guild. Only guilds with an
    ``alerts`` section are returned (whether enabled or not -- the caller checks
    ``enabled`` per kind).
    """
    return [int(gid) for gid, entry in _read().items() if entry.get("alerts")]


# ---------------------------------------------------------------------------
# Live log feed (the /log command)
# ---------------------------------------------------------------------------

_LOGFEED_FIELDS = {"enabled", "channel_id", "last_seen"}


def get_guild_logfeed(guild_id: int) -> dict:
    """Return a guild's live-log-feed settings, defaulting every key.

      * ``enabled``    -- whether the feed posts new logs automatically (default False).
      * ``channel_id`` -- the channel to post logs in (int), or ``None`` if unset.
      * ``last_seen``  -- the ``created_at`` of the newest log already posted (an
        ISO-8601 string), or ``None`` if never. The poller posts only logs newer
        than this, so it never repeats or dumps a backlog.
    """
    entry = _read().get(str(guild_id)) or {}
    settings = entry.get("logfeed") or {}
    return {
        "enabled": settings.get("enabled", False),
        "channel_id": settings.get("channel_id"),
        "last_seen": settings.get("last_seen"),
    }


def set_guild_logfeed(guild_id: int, **fields) -> None:
    """Update some of a guild's log-feed settings, leaving the rest as-is.

    Accepts any of ``enabled`` / ``channel_id`` / ``last_seen`` and merges them
    into the stored settings, preserving the guild's other keys (contest, shame,
    alerts). This partial shape lets the poller bump only ``last_seen`` without
    touching the admin's ``enabled``/``channel_id`` choices.
    """
    unknown = set(fields) - _LOGFEED_FIELDS
    if unknown:
        raise ValueError(f"unknown logfeed field(s): {sorted(unknown)}")
    data = _read()
    entry = data.get(str(guild_id), {})
    entry.setdefault("logfeed", {}).update(fields)
    data[str(guild_id)] = entry
    _write(data)


def guilds_with_logfeed() -> list[int]:
    """Return the ids of guilds that have the log feed configured.

    Lets the poller iterate just the guilds it might post for. Only guilds with a
    ``logfeed`` section are returned (whether enabled or not -- the caller checks
    ``enabled``).
    """
    return [int(gid) for gid, entry in _read().items() if entry.get("logfeed")]


# ---------------------------------------------------------------------------
# Tadoku-username <-> Discord-user claims (the /claim command family)
# ---------------------------------------------------------------------------


def get_guild_claims(guild_id: int) -> dict[str, str]:
    """Return a guild's claim map: ``{discord_user_id_str: tadoku_username}``.

    A copy, so callers can read/iterate freely without mutating stored state.
    Missing/never-claimed guilds return an empty dict. Enforcing "one claim per
    user, each username claimed once" is the caller's job (the ``claims`` cog),
    which reads this map to check both directions before writing.
    """
    entry = _read().get(str(guild_id)) or {}
    return dict(entry.get("claims") or {})


def set_claim(guild_id: int, user_id: int, username: str) -> None:
    """Link a Discord user to a tadoku username, preserving the guild's other keys.

    Overwrites any previous claim for ``user_id``; callers that require "one claim
    per user" check first. The username is stored as given (typically the
    leaderboard's canonical spelling) so it can be displayed verbatim.
    """
    data = _read()
    entry = data.get(str(guild_id), {})
    entry.setdefault("claims", {})[str(user_id)] = username
    data[str(guild_id)] = entry
    _write(data)


def remove_claim(guild_id: int, user_id: int) -> str | None:
    """Drop ``user_id``'s claim; return the username removed, or ``None`` if none.

    Returning the freed username lets the caller confirm exactly what was
    unclaimed. A no-op (user had no claim) leaves the file untouched.
    """
    data = _read()
    entry = data.get(str(guild_id))
    if not entry:
        return None
    claims = entry.get("claims") or {}
    removed = claims.pop(str(user_id), None)
    if removed is not None:
        data[str(guild_id)] = entry
        _write(data)
    return removed
