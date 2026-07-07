"""Tests for the claims cog (/claim, /unclaim, /unclaimedlist, /autoclaim).

The tadoku-facing lookups live in the leaderboard cog and are covered there, so
here we monkeypatch ``_resolve_contest`` / ``_find_leaderboard_entry`` /
``_scored_participants`` and focus on the claim logic: the two-way uniqueness
rules, the unclaimed listing, and autoclaim's name-matching (unique match only,
skip already-claimed names/members). Cog callbacks are invoked directly with a
fake interaction; guild member lookups use a fake ``query_members``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import cogs.claims as claims_cog
import cogs.leaderboard as leaderboard_cog
import lib.config_store as config_store
import lib.tadoku_client as tadoku_client
from tests.conftest import make_interaction

CONTEST = {"id": "c1", "title": "2026 Round 4"}


@pytest.fixture(autouse=True)
def patched_leaderboard(monkeypatch):
    """Stub the leaderboard cog's tadoku helpers the claims cog reuses."""
    monkeypatch.setattr(leaderboard_cog, "_resolve_contest", AsyncMock(return_value=CONTEST))
    monkeypatch.setattr(
        leaderboard_cog, "_find_leaderboard_entry", AsyncMock(return_value={"user_display_name": "Ruby "})
    )
    monkeypatch.setattr(leaderboard_cog, "_scored_participants", AsyncMock(return_value=[]))
    return leaderboard_cog


def _entry(name, score=1.0):
    return {"user_display_name": name, "score": score}


def _member(mid, name, display=None, global_name=None):
    return SimpleNamespace(id=mid, name=name, display_name=display or name, global_name=global_name)


def _guild_with_members(members):
    """Fake guild whose query_members returns members matching a name prefix."""
    async def query(query, limit=100):
        q = query.strip().casefold()
        out = []
        for m in members:
            names = {m.name.casefold(), (m.display_name or "").casefold()}
            gn = getattr(m, "global_name", None)
            if gn:
                names.add(gn.casefold())
            if any(n.startswith(q) for n in names):
                out.append(m)
        return out

    return SimpleNamespace(query_members=AsyncMock(side_effect=query))


# ---------------------------------------------------------------------------
# /claim
# ---------------------------------------------------------------------------


async def test_claim_links_caller_to_canonical_name(fake_bot):
    leaderboard_cog._find_leaderboard_entry.return_value = {"user_display_name": "Ruby "}
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.claim.callback(cog, interaction, username="ruby")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    # Stores the leaderboard's own spelling, keyed by the Discord user id.
    assert config_store.get_guild_claims(999) == {"111": "Ruby "}
    args, _ = interaction.followup.send.await_args
    assert "Ruby" in args[0]


async def test_claim_rejects_when_caller_already_has_a_claim(fake_bot):
    config_store.set_claim(999, 111, "ryun")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.claim.callback(cog, interaction, username="ruby")

    assert config_store.get_guild_claims(999) == {"111": "ryun"}  # unchanged
    args, _ = interaction.followup.send.await_args
    assert "already claimed" in args[0].lower()
    # Bailed before touching the leaderboard.
    leaderboard_cog._find_leaderboard_entry.assert_not_called()


async def test_claim_rejects_username_taken_by_another(fake_bot):
    config_store.set_claim(999, 222, "Ruby ")  # different spelling, same person
    leaderboard_cog._find_leaderboard_entry.return_value = {"user_display_name": "ruby"}
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.claim.callback(cog, interaction, username="ruby")

    assert "111" not in config_store.get_guild_claims(999)
    args, _ = interaction.followup.send.await_args
    assert "already claimed by" in args[0].lower()
    assert "222" in args[0]  # mentions <@222>


async def test_claim_rejects_name_not_on_leaderboard(fake_bot):
    leaderboard_cog._find_leaderboard_entry.return_value = None
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.claim.callback(cog, interaction, username="ghost")

    assert config_store.get_guild_claims(999) == {}
    args, _ = interaction.followup.send.await_args
    assert "isn't on the leaderboard" in args[0]


async def test_claim_reports_api_error(fake_bot):
    leaderboard_cog._resolve_contest.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.claim.callback(cog, interaction, username="ruby")

    args, _ = interaction.followup.send.await_args
    assert "tadoku.app" in args[0]


# ---------------------------------------------------------------------------
# /unclaim
# ---------------------------------------------------------------------------


async def test_unclaim_removes_and_confirms(fake_bot):
    config_store.set_claim(999, 111, "ruby")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.unclaim.callback(cog, interaction)

    assert config_store.get_guild_claims(999) == {}
    args, kwargs = interaction.response.send_message.await_args
    assert "ruby" in args[0].lower()
    assert kwargs.get("ephemeral") is True


async def test_unclaim_when_nothing_claimed(fake_bot):
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999, user_id=111)

    await cog.unclaim.callback(cog, interaction)

    args, _ = interaction.response.send_message.await_args
    assert "haven't claimed" in args[0].lower()


# ---------------------------------------------------------------------------
# /unclaimedlist
# ---------------------------------------------------------------------------


async def test_unclaimedlist_shows_only_unclaimed(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby"), _entry("ryun"), _entry("anja")]
    config_store.set_claim(999, 111, "Ruby ")  # ruby is claimed (folded match)
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.unclaimedlist.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "ryun" in embed.description and "anja" in embed.description
    assert "ruby" not in embed.description.lower()
    assert "2 unclaimed of 3" in embed.footer.text


async def test_unclaimedlist_all_claimed(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby")]
    config_store.set_claim(999, 111, "ruby")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.unclaimedlist.callback(cog, interaction)

    args, kwargs = interaction.followup.send.await_args
    assert "embed" not in kwargs
    assert "claimed" in args[0].lower()


async def test_unclaimedlist_caps_long_list(fake_bot):
    names = [f"user{i}" for i in range(claims_cog.UNCLAIMED_LIST_LIMIT + 5)]
    leaderboard_cog._scored_participants.return_value = [_entry(n) for n in names]
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.unclaimedlist.callback(cog, interaction)

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "…and 5 more" in embed.description


async def test_unclaimedlist_reports_api_error(fake_bot):
    leaderboard_cog._scored_participants.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)

    await cog.unclaimedlist.callback(cog, interaction)

    args, _ = interaction.followup.send.await_args
    assert "tadoku.app" in args[0]


# ---------------------------------------------------------------------------
# /autoclaim
# ---------------------------------------------------------------------------


async def test_autoclaim_pairs_same_named_members(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby"), _entry("ryun")]
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, "ruby"), _member(222, "ryun")])

    await cog.autoclaim.callback(cog, interaction)

    assert config_store.get_guild_claims(999) == {"111": "ruby", "222": "ryun"}
    args, _ = interaction.followup.send.await_args
    assert "2" in args[0]


async def test_autoclaim_matches_by_display_name(fake_bot):
    # Tadoku name matches the member's nickname, not their username.
    leaderboard_cog._scored_participants.return_value = [_entry("Ruby")]
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, name="xX_r_Xx", display="Ruby")])

    await cog.autoclaim.callback(cog, interaction)

    assert config_store.get_guild_claims(999) == {"111": "Ruby"}


async def test_autoclaim_skips_already_claimed_username(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby")]
    config_store.set_claim(999, 500, "ruby")  # already claimed by someone
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, "ruby")])

    await cog.autoclaim.callback(cog, interaction)

    # The existing claim stands; no new pairing for member 111.
    assert config_store.get_guild_claims(999) == {"500": "ruby"}


async def test_autoclaim_skips_member_who_already_claimed(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby")]
    config_store.set_claim(999, 111, "othername")  # member 111 already linked
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, "ruby")])

    await cog.autoclaim.callback(cog, interaction)

    # Not overwritten, and "ruby" isn't added under member 111.
    assert config_store.get_guild_claims(999) == {"111": "othername"}


async def test_autoclaim_skips_ambiguous_name(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ruby")]
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, "ruby"), _member(222, "ruby")])

    await cog.autoclaim.callback(cog, interaction)

    assert config_store.get_guild_claims(999) == {}


async def test_autoclaim_skips_when_no_member_matches(fake_bot):
    leaderboard_cog._scored_participants.return_value = [_entry("ghost")]
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([_member(111, "ruby")])

    await cog.autoclaim.callback(cog, interaction)

    assert config_store.get_guild_claims(999) == {}


async def test_autoclaim_reports_api_error(fake_bot):
    leaderboard_cog._resolve_contest.side_effect = tadoku_client.TadokuAPIError("boom")
    cog = claims_cog.Claims(fake_bot)
    interaction = make_interaction(guild_id=999)
    interaction.guild = _guild_with_members([])

    await cog.autoclaim.callback(cog, interaction)

    args, _ = interaction.followup.send.await_args
    assert "tadoku.app" in args[0]
