"""Per-guild "which contest is displayed" setting, persisted as JSON.

The only local state this bot keeps -- everything else is fetched live
from tadoku.app. Writes go to a temp file and are renamed into place so a
crash mid-write can't corrupt config.json.
"""

import json
import os
from typing import Optional

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")


def _read() -> dict:
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r") as f:
        return json.load(f)


def _write(data: dict) -> None:
    tmp_path = f"{_PATH}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _PATH)


def get_guild_contest(guild_id: int) -> Optional[dict]:
    """Returns {"contest_id": ..., "contest_title": ...} or None if unset."""
    return _read().get(str(guild_id))


def set_guild_contest(guild_id: int, contest_id: str, contest_title: str) -> None:
    data = _read()
    data[str(guild_id)] = {"contest_id": contest_id, "contest_title": contest_title}
    _write(data)
