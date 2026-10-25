"""Tests for the tadoku.app API client.

Drives every client function against the ``tadoku_server`` fixture (a real
loopback HTTP server), asserting both on the parsed return value and on the
exact path/query the client sent -- so a wrong URL or a mistyped query param
fails the test. Also covers the error path (404 -> TadokuAPIError) and the
bool-to-string query coercion.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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


async def test_list_contest_logs_returns_logs_and_sends_paging(tadoku_server):
    tadoku_server.set_response(
        "/contests/abc-123/logs",
        200,
        {"logs": [{"user_id": "u1", "score": 6, "created_at": "2026-07-01T22:56:46Z"}], "total_size": 1},
    )

    async with aiohttp.ClientSession() as session:
        logs = await tadoku.list_contest_logs(session, "abc-123", page=2, page_size=100)

    assert logs[0]["user_id"] == "u1"
    [(path, query)] = tadoku_server.requests
    assert path == "/contests/abc-123/logs"
    assert query == {"page": "2", "page_size": "100"}


async def test_list_contest_logs_defaults_to_empty_list_when_key_missing(tadoku_server):
    tadoku_server.set_response("/contests/abc-123/logs", 200, {})

    async with aiohttp.ClientSession() as session:
        logs = await tadoku.list_contest_logs(session, "abc-123")

    assert logs == []


async def test_list_user_logs_returns_envelope_and_sends_paging(tadoku_server):
    tadoku_server.set_response(
        "/users/user-1/logs",
        200,
        {"logs": [{"amount": 12, "unit_name": "Page"}], "total_size": 24},
    )

    async with aiohttp.ClientSession() as session:
        data = await tadoku.list_user_logs(session, "user-1", page=1, page_size=100)

    assert data["total_size"] == 24
    assert data["logs"][0]["unit_name"] == "Page"
    [(path, query)] = tadoku_server.requests
    assert path == "/users/user-1/logs"
    assert query == {"page": "1", "page_size": "100"}


async def test_get_contest_events_returns_page_and_sends_cursor_and_limit(tadoku_server):
    page = {
        "events": [{"id": "e1", "log_id": "l1", "event": "log.submitted",
                    "occurred_at": "2026-10-24T11:59:38.602024Z"}],
        "next_cursor": "e1",
        "has_more": False,
    }
    tadoku_server.set_response("/contests/abc-123/events", 200, page)

    async with aiohttp.ClientSession() as session:
        data = await tadoku.get_contest_events(session, "abc-123", cursor="e0", limit=50)

    assert data == page
    assert tadoku_server.requests == [("/contests/abc-123/events", {"cursor": "e0", "limit": "50"})]


async def test_get_contest_events_raises_with_status_on_bad_cursor(tadoku_server):
    tadoku_server.set_response("/contests/abc-123/events", 400, {"error": "bad cursor"})

    async with aiohttp.ClientSession() as session:
        with pytest.raises(tadoku.TadokuAPIError) as err:
            await tadoku.get_contest_events(session, "abc-123", cursor="garbage")

    assert err.value.status == 400


async def test_get_contest_log_returns_current_log(tadoku_server):
    log = {"id": "l1", "user_display_name": "mudang", "score": 50.4}
    tadoku_server.set_response("/contests/abc-123/logs/l1", 200, log)

    async with aiohttp.ClientSession() as session:
        assert await tadoku.get_contest_log(session, "abc-123", "l1") == log

    assert tadoku_server.requests == [("/contests/abc-123/logs/l1", {})]


async def test_get_contest_log_returns_none_when_log_left_the_contest(tadoku_server):
    tadoku_server.set_response("/contests/abc-123/logs/gone", 404, {"error": "not found"})

    async with aiohttp.ClientSession() as session:
        assert await tadoku.get_contest_log(session, "abc-123", "gone") is None


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_get_contest_log_raises_on_non_404_errors(tadoku_server, status):
    # Only "not in the contest" means withdrawn; anything else must not look like it.
    tadoku_server.set_response("/contests/abc-123/logs/l1", status, {})

    async with aiohttp.ClientSession() as session:
        with pytest.raises(tadoku.TadokuAPIError) as err:
            await tadoku.get_contest_log(session, "abc-123", "l1")

    assert err.value.status == status


async def test_get_contest_log_raises_when_still_unauthorized_after_relogin(monkeypatch):
    monkeypatch.setattr(tadoku, "_auth", _fake_auth())
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(side_effect=[(401, None), (401, None)]))

    with pytest.raises(tadoku.TadokuAPIError) as err:
        await tadoku.get_contest_log(SimpleNamespace(), "abc-123", "l1")

    assert err.value.status == 401


async def test_transport_failure_has_no_status(monkeypatch):
    async def _boom(*args, **kwargs):
        raise tadoku.TadokuAPIError("Could not reach tadoku.app: boom")
    monkeypatch.setattr(tadoku, "_fetch", _boom)

    with pytest.raises(tadoku.TadokuAPIError) as err:
        await tadoku.get_contest_log(SimpleNamespace(), "abc-123", "l1")

    assert err.value.status is None


# ---------------------------------------------------------------------------
# authentication (native session): login-before-first-call + 401 retry
# ---------------------------------------------------------------------------

def _fake_auth():
    return SimpleNamespace(ensure_login=AsyncMock(return_value=(1, "TOKEN")))


async def test_get_logs_in_before_the_first_request(monkeypatch):
    auth = _fake_auth()
    monkeypatch.setattr(tadoku, "_auth", auth)
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(return_value=(200, {"ok": True})))

    data = await tadoku._get(SimpleNamespace(), "/x")

    assert data == {"ok": True}
    auth.ensure_login.assert_awaited()  # ensured a session before fetching


async def test_get_refreshes_session_and_retries_on_401(monkeypatch):
    auth = _fake_auth()
    monkeypatch.setattr(tadoku, "_auth", auth)
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(side_effect=[(401, None), (200, {"ok": 1})]))

    data = await tadoku._get(SimpleNamespace(), "/x")

    assert data == {"ok": 1}
    assert tadoku._fetch.await_count == 2
    # Refresh identifies the rejected session generation.
    assert auth.ensure_login.await_count == 2
    assert auth.ensure_login.await_args_list[1].kwargs.get("rejected_generation") == 1


async def test_get_raises_if_still_unauthorized_after_retry(monkeypatch):
    monkeypatch.setattr(tadoku, "_auth", _fake_auth())
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(side_effect=[(401, None), (401, None)]))

    with pytest.raises(tadoku.TadokuAPIError):
        await tadoku._get(SimpleNamespace(), "/x")
    assert tadoku._fetch.await_count == 2


async def test_get_does_not_refresh_on_403(monkeypatch):
    auth = _fake_auth()
    monkeypatch.setattr(tadoku, "_auth", auth)
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(return_value=(403, None)))
    with pytest.raises(tadoku.TadokuAPIError):
        await tadoku._get(SimpleNamespace(), "/x")
    auth.ensure_login.assert_awaited_once()
    tadoku._fetch.assert_awaited_once()


async def test_retry_uses_the_new_session_token(monkeypatch):
    auth = _fake_auth()
    auth.ensure_login.side_effect = [(1, "old"), (2, "new")]
    monkeypatch.setattr(tadoku, "_auth", auth)
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(side_effect=[(401, None), (200, {})]))
    await tadoku._get(SimpleNamespace(), "/x")
    assert [call.kwargs["token"] for call in tadoku._fetch.await_args_list] == ["old", "new"]


async def test_get_without_auth_does_not_retry_on_401(monkeypatch):
    # _auth defaults to None (reset fixture): a 401 just raises, no retry.
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(side_effect=[(401, None)]))

    with pytest.raises(tadoku.TadokuAPIError):
        await tadoku._get(SimpleNamespace(), "/x")
    assert tadoku._fetch.await_count == 1


async def test_get_surfaces_login_failure_as_api_error(monkeypatch):
    auth = SimpleNamespace(ensure_login=AsyncMock(side_effect=tadoku.TadokuAuthError("nope")))
    monkeypatch.setattr(tadoku, "_auth", auth)
    monkeypatch.setattr(tadoku, "_fetch", AsyncMock(return_value=(200, {})))

    with pytest.raises(tadoku.TadokuAPIError) as err:
        await tadoku._get(SimpleNamespace(), "/x")
    assert err.value.status is None
