"""Native identity protocol and persistent-session lifecycle, using a local server."""

import asyncio
import json
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from lib import tadoku_auth as authmod, tadoku_client as client


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(authmod, "SESSION_FILE", tmp_path / "session.json")
    for key in ("TADOKU_EMAIL", "TADOKU_PASSWORD", "KRATOS_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
async def identity_server():
    state = SimpleNamespace(logins=0, validations=0, status=200, validation_status=200,
                            account={"id": "user-1", "email": "bot@example.com"},
                            valid=True, cookie=True, retry_after=None, max_age="2592000",
                            requests=[], token="", api_status=200)

    async def login(request):
        state.logins += 1
        state.requests.append((request.path, dict(request.headers), await request.json()))
        headers = {"Retry-After": state.retry_after} if state.retry_after else {}
        response = web.json_response({"account": state.account, "account_app": "v2"},
                                     status=state.status, headers=headers)
        if state.cookie:
            state.token = f"secret-token-{state.logins}"
            response.set_cookie("tadoku_session", state.token, max_age=state.max_age, httponly=True)
        return response

    async def validate(request):
        state.validations += 1
        state.requests.append((request.path, dict(request.headers), None))
        return web.json_response({"account": state.account if state.valid else None},
                                 status=state.validation_status)

    async def api(request):
        state.requests.append((request.path, dict(request.headers), None))
        return web.json_response({"ok": True}, status=state.api_status)

    app = web.Application()
    app.router.add_post('/identity/login', login)
    app.router.add_get('/identity/session', validate)
    app.router.add_get('/immersion/x', api)
    app.router.add_get('/poster', api)
    server = TestServer(app)
    await server.start_server()
    state.url = str(server.make_url('')).rstrip('/')
    try:
        yield state
    finally:
        await server.close()


def manager(server):
    return authmod.TadokuAuth("bot@example.com", "PASSWORD", identity_url=server.url + "/identity")


def session():
    # Production deliberately avoids automatic cookies on the shared session.
    return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())


def test_environment_defaults_and_obsolete_warning(monkeypatch, caplog):
    assert authmod.TadokuAuth.from_env() is None
    monkeypatch.setenv("KRATOS_PUBLIC_URL", "old-value")
    assert authmod.TadokuAuth.from_env() is None
    assert "obsolete" in caplog.text
    monkeypatch.setenv("TADOKU_EMAIL", "bot@example.com")
    with pytest.raises(authmod.TadokuAuthError, match="both"):
        authmod.TadokuAuth.from_env()
    monkeypatch.setenv("TADOKU_PASSWORD", "PASSWORD")
    assert authmod.TadokuAuth.from_env().identity_url == authmod.IDENTITY_URL
    monkeypatch.delenv("TADOKU_EMAIL")
    with pytest.raises(authmod.TadokuAuthError, match="both"):
        authmod.TadokuAuth.from_env()


async def test_login_protocol_and_atomic_private_storage(identity_server, caplog):
    server = identity_server
    async with session() as http:
        auth = manager(server)
        generation, token = await auth.ensure_login(http)
        assert await auth.ensure_login(http) == (generation, token)
        assert not list(http.cookie_jar)
    assert server.logins == 1
    path, headers, body = server.requests[0]
    assert path == "/identity/login"
    assert headers["Origin"] == "https://tadoku.app"
    assert headers["Content-Type"] == "application/json"
    assert body == {"email": "bot@example.com", "password": "PASSWORD"}
    saved = json.loads(auth.session_file.read_text())
    assert saved["token"] == token
    assert saved["expires_at"] > authmod.time.time()
    assert auth.session_file.stat().st_mode & 0o777 == 0o600
    assert list(auth.session_file.parent.glob("*.tmp")) == []
    assert "PASSWORD" not in auth.session_file.read_text()
    assert "PASSWORD" not in caplog.text and token not in caplog.text


async def test_restart_validates_cookie_without_login(identity_server):
    async with session() as http:
        _, token = await manager(identity_server).ensure_login(http)
    async with session() as http:
        auth = manager(identity_server)
        assert (await auth.ensure_login(http))[1] == token
        await auth.ensure_login(http)
    assert identity_server.logins == 1
    assert identity_server.validations == 1
    assert identity_server.requests[-1][1]["Cookie"] == f"tadoku_session={token}"


async def test_null_session_relogs_in(identity_server):
    async with session() as http:
        await manager(identity_server).ensure_login(http)
        identity_server.valid = False
        await manager(identity_server).ensure_login(http)
    assert identity_server.validations == 1 and identity_server.logins == 2


async def test_validation_failure_does_not_trigger_login(identity_server):
    async with session() as http:
        await manager(identity_server).ensure_login(http)
        identity_server.validation_status = 503
        auth = manager(identity_server)
        for _ in range(3):
            with pytest.raises(authmod.TadokuAuthError):
                await auth.ensure_login(http)
    assert identity_server.validations == 1 and identity_server.logins == 1


@pytest.mark.parametrize("contents", ["not json", "[]", '{"email": null}',
    json.dumps({"email": "someone@example.com", "token": "old", "expires_at": 9999999999}),
    json.dumps({"email": "bot@example.com", "token": "old", "expires_at": 1}),
    json.dumps({"email": "bot@example.com", "token": 5, "expires_at": 9999999999}),
    json.dumps({"email": "bot@example.com", "token": "old", "expires_at": float("inf")}),
])
async def test_invalid_or_unusable_saved_session_is_replaced(identity_server, contents):
    authmod.SESSION_FILE.write_text(contents)
    async with session() as http:
        await manager(identity_server).ensure_login(http)
    assert identity_server.logins == 1 and identity_server.validations == 0


async def test_local_expiry_renews_before_request(identity_server, monkeypatch):
    now = authmod.time.time()
    monkeypatch.setattr(authmod.time, "time", lambda: now)
    async with session() as http:
        auth = manager(identity_server)
        await auth.ensure_login(http)
        now += authmod.SESSION_SECONDS + 1
        await auth.ensure_login(http)
    assert identity_server.logins == 2


async def test_concurrent_initial_and_rejected_requests_share_login(identity_server):
    async with session() as http:
        auth = manager(identity_server)
        initial = await asyncio.gather(*(auth.ensure_login(http) for _ in range(10)))
        assert len(set(initial)) == 1 and identity_server.logins == 1
        refreshed = await asyncio.gather(*(
            auth.ensure_login(http, rejected_generation=initial[0][0]) for _ in range(10)))
        assert len(set(refreshed)) == 1 and identity_server.logins == 2
        assert refreshed[0][0] != initial[0][0]


async def test_concurrent_api_401s_refresh_once(identity_server, monkeypatch):
    auth = manager(identity_server)
    monkeypatch.setattr(client, "_auth", auth)
    arrived = 0
    gate = asyncio.Event()

    async def fetch(http, url, params, *, token=None):
        nonlocal arrived
        if token == "secret-token-1":
            arrived += 1
            if arrived == 8:
                gate.set()
            await gate.wait()
            return 401, None
        assert token == "secret-token-2"
        return 200, {"ok": True}

    monkeypatch.setattr(client, "_fetch", fetch)
    async with session() as http:
        results = await asyncio.wait_for(
            asyncio.gather(*(client._get(http, "/x") for _ in range(8))), timeout=5
        )
    assert results == [{"ok": True}] * 8
    assert identity_server.logins == 2


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_configuration_errors_disable_login_until_restart(identity_server, status):
    identity_server.status = status
    async with session() as http:
        auth = manager(identity_server)
        for _ in range(3):
            with pytest.raises(authmod.TadokuAuthError, match="restart"):
                await auth.ensure_login(http)
    assert identity_server.logins == 1


@pytest.mark.parametrize("retry_after, seconds", [(None, 900), ("1", 900), ("1800", 1800), ("nonsense", 900)])
async def test_throttle_is_persistent(identity_server, monkeypatch, retry_after, seconds):
    now = authmod.time.time()
    monkeypatch.setattr(authmod.time, "time", lambda: now)
    identity_server.status = 429
    identity_server.retry_after = retry_after
    async with session() as http:
        auth = manager(identity_server)
        with pytest.raises(authmod.TadokuAuthError):
            await auth.ensure_login(http)
        assert auth._retry_at == now + seconds
        restarted = manager(identity_server)
        with pytest.raises(authmod.TadokuAuthError, match="paused"):
            await restarted.ensure_login(http)
        assert identity_server.logins == 1
        now += seconds + 1
        identity_server.status = 200
        await restarted.ensure_login(http)
        assert identity_server.logins == 2


async def test_transient_backoff_grows_and_resets(identity_server, monkeypatch):
    now = authmod.time.time()
    monkeypatch.setattr(authmod.time, "time", lambda: now)
    identity_server.status = 503
    async with session() as http:
        auth = manager(identity_server)
        for delay in (30, 60, 120, 240, 480, 900, 900):
            with pytest.raises(authmod.TadokuAuthError):
                await auth.ensure_login(http)
            assert auth._retry_at == now + delay
            with pytest.raises(authmod.TadokuAuthError, match="paused"):
                await auth.ensure_login(http)
            now += delay + 1
        identity_server.status = 200
        await auth.ensure_login(http)
        assert auth._retry_at == 0 and auth._failures == 0
    assert identity_server.logins == 8


def test_retry_after_accepts_http_dates(monkeypatch):
    monkeypatch.setattr(authmod.time, "time", lambda: 1000000000)
    assert authmod.TadokuAuth._throttle_delay(formatdate(1000003600, usegmt=True)) == 3600


@pytest.mark.parametrize("broken", ["account", "cookie", "expiry"])
async def test_success_requires_account_cookie_and_valid_expiry(identity_server, broken):
    if broken == "account":
        identity_server.account = None
    elif broken == "cookie":
        identity_server.cookie = False
    else:
        identity_server.max_age = "0"
    async with session() as http:
        auth = manager(identity_server)
        with pytest.raises(authmod.TadokuAuthError):
            await auth.ensure_login(http)
        assert auth._token is None


async def test_persistence_failure_retains_in_memory_session(identity_server, monkeypatch, caplog):
    authmod.SESSION_FILE.write_text('{"previous": "complete state"}')
    monkeypatch.setattr(authmod.os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    async with session() as http:
        auth = manager(identity_server)
        first = await auth.ensure_login(http)
        assert await auth.ensure_login(http) == first
    assert identity_server.logins == 1
    assert "in-memory" in caplog.text
    assert json.loads(authmod.SESSION_FILE.read_text()) == {"previous": "complete state"}
    assert list(auth.session_file.parent.glob("*.tmp")) == []


async def test_authenticated_api_cookie_is_not_sent_to_poster_requests(identity_server, monkeypatch):
    monkeypatch.setattr(client, "BASE_URL", identity_server.url + "/immersion")
    monkeypatch.setattr(client, "_auth", manager(identity_server))
    async with session() as http:
        assert await client._get(http, "/x") == {"ok": True}
        async with http.get(identity_server.url + "/poster") as response:
            assert response.status == 200
    api_headers = identity_server.requests[-2][1]
    poster_headers = identity_server.requests[-1][1]
    assert api_headers["Cookie"] == "tadoku_session=secret-token-1"
    assert "Cookie" not in poster_headers
    for headers in (api_headers, poster_headers):
        assert "Authorization" not in headers and "X-Session-Token" not in headers


async def test_transport_failure_is_sanitized_and_backed_off():
    http = SimpleNamespace(post=lambda *a, **kw: (_ for _ in ()).throw(aiohttp.ClientError("SECRET")))
    auth = authmod.TadokuAuth("bot@example.com", "PASSWORD")
    with pytest.raises(authmod.TadokuAuthError) as error:
        await auth.ensure_login(http)
    assert "SECRET" not in str(error.value)
    assert auth._retry_at > authmod.time.time()
