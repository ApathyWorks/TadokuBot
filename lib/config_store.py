"""Per-guild "which contest is displayed" setting, persisted as JSON.

This is the *only* local state the bot keeps -- everything else (contest
details, scores, rankings) is fetched live from tadoku.app on demand. The
store is a single JSON object keyed by Discord guild id (as a string, since
JSON object keys are always strings):

    {
      "123456789": {"contest_id": "<uuid>", "contest_title": "2026 Round 4"}
    }

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
    return _read().get(str(guild_id))


def set_guild_contest(guild_id: int, contest_id: str, contest_title: str) -> None:
    """Pin ``guild_id`` to a specific contest, overwriting any previous choice.

    We store the title alongside the id so ``/current_contest`` can name the
    contest without an extra API round-trip.
    """
    # Read-modify-write the whole object: load current state, update this one
    # guild's entry, write it all back atomically.
    data = _read()
    data[str(guild_id)] = {"contest_id": contest_id, "contest_title": contest_title}
    _write(data)
