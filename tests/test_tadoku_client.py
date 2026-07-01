"""Tests for the tadoku.app API client.

Drives every client function against the ``tadoku_server`` fixture (a real
loopback HTTP server), asserting both on the parsed return value and on the
exact path/query the client sent -- so a wrong URL or a mistyped query param
fails the test. Also covers the error path (404 -> TadokuAPIError) and the
bool-to-string query coercion.
"""

import aiohttp
import pytest

import lib.tadoku_client as tadoku


async def test_get_latest_official_contest_parses_response(tadoku_server):
    tadoku_server.set_response(
        "/contests/latest-official",
        200,
        {"id": "contest-1", "title": "2026 Round 4", "contest_start": "2026-07-01", "contest_end": "2026-07-31"},
    )

    async with aiohttp.ClientSession() as session:
        contest = await tadoku.get_latest_official_contest(session)

    assert contest["id"] == "contest-1"
    assert contest["title"] == "2026 Round 4"
    assert tadoku_server.requests == [("/contests/latest-official", {})]


async def test_get_contest_hits_correct_path(tadoku_server):
    tadoku_server.set_response("/contests/abc-123", 200, {"id": "abc-123", "title": "Some Contest"})

    async with aiohttp.ClientSession() as session:
        contest = await tadoku.get_contest(session, "abc-123")

    assert contest["title"] == "Some Contest"
    assert tadoku_server.requests == [("/contests/abc-123", {})]


async def test_get_contest_raises_tadoku_api_error_on_404(tadoku_server):
    tadoku_server.set_response("/contests/missing", 404, {"error": "not found"})

    async with aiohttp.ClientSession() as session:
        with pytest.raises(tadoku.TadokuAPIError):
            await tadoku.get_contest(session, "missing")


async def test_get_contest_leaderboard_sends_page_and_page_size(tadoku_server):
    tadoku_server.set_response(
        "/contests/abc-123/leaderboard",
        200,
        {"entries": [{"rank": 1, "user_id": "u1", "user_display_name": "ruby", "score": 177.1, "is_tie": False}],
         "total_size": 1},
    )

    async with aiohttp.ClientSession() as session:
        data = await tadoku.get_contest_leaderboard(session, "abc-123", page=2, page_size=5)

    assert data["entries"][0]["user_display_name"] == "ruby"
    [(path, query)] = tadoku_server.requests
    assert path == "/contests/abc-123/leaderboard"
    assert query == {"page": "2", "page_size": "5"}


async def test_get_contest_leaderboard_omits_unset_filters_from_query(tadoku_server):
    tadoku_server.set_response("/contests/abc-123/leaderboard", 200, {"entries": [], "total_size": 0})

    async with aiohttp.ClientSession() as session:
        await tadoku.get_contest_leaderboard(session, "abc-123")

    [(_, query)] = tadoku_server.requests
    assert "language_code" not in query
    assert "activity_id" not in query


async def test_get_contest_leaderboard_includes_language_and_activity_filters(tadoku_server):
    tadoku_server.set_response("/contests/abc-123/leaderboard", 200, {"entries": [], "total_size": 0})

    async with aiohttp.ClientSession() as session:
        await tadoku.get_contest_leaderboard(
            session, "abc-123", language_code="jpa", activity_id=1
        )

    [(_, query)] = tadoku_server.requests
    assert query["language_code"] == "jpa"
    assert query["activity_id"] == "1"


async def test_list_contests_returns_contests_list(tadoku_server):
    tadoku_server.set_response(
        "/contests",
        200,
        {"contests": [{"id": "c1", "title": "Round 1"}, {"id": "c2", "title": "Round 2"}]},
    )

    async with aiohttp.ClientSession() as session:
        contests = await tadoku.list_contests(session)

    assert [c["id"] for c in contests] == ["c1", "c2"]


async def test_list_contests_sends_official_filter(tadoku_server):
    tadoku_server.set_response("/contests", 200, {"contests": []})

    async with aiohttp.ClientSession() as session:
        await tadoku.list_contests(session, official=True, page=3, page_size=10)

    [(_, query)] = tadoku_server.requests
    assert query == {"official": "true", "page": "3", "page_size": "10"}


async def test_list_contests_defaults_to_empty_list_when_key_missing(tadoku_server):
    tadoku_server.set_response("/contests", 200, {})

    async with aiohttp.ClientSession() as session:
        contests = await tadoku.list_contests(session)

    assert contests == []
