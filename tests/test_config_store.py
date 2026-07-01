"""Tests for the per-guild JSON config store.

Covers the get/set round-trip, per-guild isolation, overwrite semantics, the
missing-file default, and the atomic-write guarantee (no leftover temp file).
The ``isolated_config_store`` autouse fixture points ``_PATH`` at a temp file,
so these never touch the real data/config.json.
"""

import json
import os

import lib.config_store as config_store


def test_get_guild_contest_returns_none_when_unset():
    assert config_store.get_guild_contest(12345) is None


def test_set_then_get_round_trip():
    config_store.set_guild_contest(12345, "contest-abc", "Test Contest")

    assert config_store.get_guild_contest(12345) == {
        "contest_id": "contest-abc",
        "contest_title": "Test Contest",
    }


def test_different_guilds_are_independent():
    config_store.set_guild_contest(1, "contest-a", "Contest A")
    config_store.set_guild_contest(2, "contest-b", "Contest B")

    assert config_store.get_guild_contest(1)["contest_id"] == "contest-a"
    assert config_store.get_guild_contest(2)["contest_id"] == "contest-b"


def test_set_guild_contest_overwrites_previous_value():
    config_store.set_guild_contest(1, "contest-a", "Contest A")
    config_store.set_guild_contest(1, "contest-b", "Contest B")

    assert config_store.get_guild_contest(1) == {
        "contest_id": "contest-b",
        "contest_title": "Contest B",
    }


def test_write_is_atomic_no_leftover_tmp_file():
    config_store.set_guild_contest(1, "contest-a", "Contest A")

    assert os.path.exists(config_store._PATH)
    assert not os.path.exists(f"{config_store._PATH}.tmp")


def test_file_contents_are_valid_json_keyed_by_guild_id_string():
    config_store.set_guild_contest(555, "contest-a", "Contest A")

    with open(config_store._PATH) as f:
        data = json.load(f)

    assert data == {"555": {"contest_id": "contest-a", "contest_title": "Contest A"}}
