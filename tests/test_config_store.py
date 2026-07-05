"""Tests for the per-guild JSON config store.

Covers the get/set round-trip, per-guild isolation, overwrite semantics, the
missing-file default, and the atomic-write guarantee (no leftover temp file).
The ``isolated_config_store`` autouse fixture points ``_PATH`` at a temp file,
so these never touch the real data/config.json.
"""

import json
import os

import pytest

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


# ---------------------------------------------------------------------------
# shame toggle
# ---------------------------------------------------------------------------


def test_get_guild_shame_defaults_to_true_when_unset():
    assert config_store.get_guild_shame(12345) is True


def test_set_then_get_shame_round_trip():
    config_store.set_guild_shame(12345, False)
    assert config_store.get_guild_shame(12345) is False

    config_store.set_guild_shame(12345, True)
    assert config_store.get_guild_shame(12345) is True


def test_shame_defaults_true_for_guild_with_only_a_contest_set():
    # A guild pinned before this setting existed has no "shame" key; it should
    # still read as on.
    config_store.set_guild_contest(1, "contest-a", "Contest A")

    assert config_store.get_guild_shame(1) is True


def test_setting_shame_preserves_the_pinned_contest():
    config_store.set_guild_contest(1, "contest-a", "Contest A")
    config_store.set_guild_shame(1, False)

    assert config_store.get_guild_contest(1)["contest_id"] == "contest-a"
    assert config_store.get_guild_shame(1) is False


def test_setting_contest_preserves_the_shame_toggle():
    config_store.set_guild_shame(1, False)
    config_store.set_guild_contest(1, "contest-a", "Contest A")

    assert config_store.get_guild_shame(1) is False
    assert config_store.get_guild_contest(1)["contest_id"] == "contest-a"


# ---------------------------------------------------------------------------
# wrap-up alerts
# ---------------------------------------------------------------------------


def test_get_guild_alert_defaults_when_unset():
    assert config_store.get_guild_alert(12345, "weekly") == {
        "enabled": False,
        "channel_id": None,
        "last_period": None,
    }


def test_set_and_get_alert_round_trip():
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=42, last_period=[2026, 27])

    assert config_store.get_guild_alert(1, "weekly") == {
        "enabled": True,
        "channel_id": 42,
        "last_period": [2026, 27],
    }


def test_set_alert_merges_partial_updates():
    config_store.set_guild_alert(1, "monthly", enabled=True, channel_id=99)
    # A later update of just last_period must keep enabled/channel_id.
    config_store.set_guild_alert(1, "monthly", last_period=[2026, 6])

    assert config_store.get_guild_alert(1, "monthly") == {
        "enabled": True,
        "channel_id": 99,
        "last_period": [2026, 6],
    }


def test_weekly_and_monthly_alerts_are_independent():
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=1)
    config_store.set_guild_alert(1, "monthly", enabled=False, channel_id=2)

    assert config_store.get_guild_alert(1, "weekly")["enabled"] is True
    assert config_store.get_guild_alert(1, "monthly")["enabled"] is False
    assert config_store.get_guild_alert(1, "weekly")["channel_id"] == 1
    assert config_store.get_guild_alert(1, "monthly")["channel_id"] == 2


def test_yearly_alert_round_trip():
    config_store.set_guild_alert(1, "yearly", enabled=True, channel_id=8, last_period=[2026])

    assert config_store.get_guild_alert(1, "yearly") == {
        "enabled": True,
        "channel_id": 8,
        "last_period": [2026],
    }
    # Independent from the other kinds.
    assert config_store.get_guild_alert(1, "weekly")["enabled"] is False


def test_alerts_preserve_contest_and_shame():
    config_store.set_guild_contest(1, "contest-a", "Contest A")
    config_store.set_guild_shame(1, False)
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=7)

    assert config_store.get_guild_contest(1)["contest_id"] == "contest-a"
    assert config_store.get_guild_shame(1) is False
    assert config_store.get_guild_alert(1, "weekly")["channel_id"] == 7


def test_setting_contest_preserves_alerts():
    config_store.set_guild_alert(1, "weekly", enabled=True, channel_id=7)
    config_store.set_guild_contest(1, "contest-a", "Contest A")

    assert config_store.get_guild_alert(1, "weekly")["enabled"] is True


def test_guilds_with_alerts_lists_only_configured_guilds():
    config_store.set_guild_contest(1, "contest-a", "Contest A")  # no alerts
    config_store.set_guild_alert(2, "weekly", enabled=True, channel_id=1)
    config_store.set_guild_alert(3, "monthly", enabled=False, channel_id=2)

    assert sorted(config_store.guilds_with_alerts()) == [2, 3]


def test_alert_accessors_reject_unknown_kind():
    with pytest.raises(ValueError):
        config_store.get_guild_alert(1, "daily")
    with pytest.raises(ValueError):
        config_store.set_guild_alert(1, "daily", enabled=True)


def test_set_alert_rejects_unknown_field():
    with pytest.raises(ValueError):
        config_store.set_guild_alert(1, "weekly", bogus=True)
