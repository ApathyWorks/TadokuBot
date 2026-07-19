"""Tests for the tadoku.app Kratos login-session manager (lib.tadoku_auth).

The login flow is driven against a real loopback aiohttp server standing in for
Ory Kratos, so the actual request/cookie path runs. ``from_env`` and the CSRF
extraction are covered directly.
"""

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import lib.tadoku_auth as tadoku_auth


@pytest.fixture
async def kratos_server():
    """A fake Kratos: serves a login flow and accepts the credential submit,
    setting ``ory_kratos_session``. Yields ``(base_url, recorded)``."""
    recorded: dict = {"submits": 0}

    async def browser(request: web.Request) -> web.Response:
        return web.json_response({
            "id": "flow-1",
            "ui": {
                "action": str(request.url.with_path("/submit")),
                "nodes": [
                    {"attributes": {"name": "csrf_token", "value": "CSRF123"}},
                    {"attributes": {"name": "identifier"}},
                ],
            },
        })

    async def submit(request: web.Request) -> web.Response:
        recorded["submits"] += 1
        recorded["body"] = await request.json()
        resp = web.json_response({"session": {"id": "s1"}})
        resp.set_cookie("ory_kratos_session", "COOKIEVAL")
        return resp

    app = web.Application()
    app.router.add_get("/self-service/login/browser", browser)
    app.router.add_post("/submit", submit)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/"), recorded
    finally:
        await server.close()


def _session():
    # unsafe=True so cookies from the 127.0.0.1 loopback are kept (a real
    # tadoku.app domain doesn't need this).
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


# ---------------------------------------------------------------------------
# from_env / _find_csrf_token
# ---------------------------------------------------------------------------

def test_from_env_is_none_without_credentials(monkeypatch):
    monkeypatch.delenv("TADOKU_EMAIL", raising=False)
    monkeypatch.delenv("TADOKU_PASSWORD", raising=False)
    assert tadoku_auth.KratosAuth.from_env() is None


def test_from_env_builds_with_credentials_and_default_url(monkeypatch):
    monkeypatch.setenv("TADOKU_EMAIL", "bot@example.com")
    monkeypatch.setenv("TADOKU_PASSWORD", "pw")
    monkeypatch.delenv("KRATOS_PUBLIC_URL", raising=False)

    auth = tadoku_auth.KratosAuth.from_env()

    assert auth.email == "bot@example.com" and auth.password == "pw"
    assert auth.kratos_url == tadoku_auth.DEFAULT_KRATOS_URL


def test_from_env_custom_kratos_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("TADOKU_EMAIL", "a")
    monkeypatch.setenv("TADOKU_PASSWORD", "p")
    monkeypatch.setenv("KRATOS_PUBLIC_URL", "https://k.example/kratos/")

    assert tadoku_auth.KratosAuth.from_env().kratos_url == "https://k.example/kratos"


def test_find_csrf_token():
    nodes = [{"attributes": {"name": "identifier"}},
             {"attributes": {"name": "csrf_token", "value": "X"}}]
    assert tadoku_auth._find_csrf_token(nodes) == "X"
    assert tadoku_auth._find_csrf_token([]) is None


# ---------------------------------------------------------------------------
# login flow
# ---------------------------------------------------------------------------

async def test_login_sends_credentials_and_stores_session_cookie(kratos_server):
    base, recorded = kratos_server
    auth = tadoku_auth.KratosAuth("bot@example.com", "secret", kratos_url=base)

    async with _session() as session:
        await auth.ensure_login(session)
        cookies = {c.key: c.value for c in session.cookie_jar}

    assert cookies.get("ory_kratos_session") == "COOKIEVAL"
    body = recorded["body"]
    assert body["method"] == "password"
    assert body["identifier"] == "bot@example.com"
    assert body["password"] == "secret"
    assert body["csrf_token"] == "CSRF123"  # taken from the flow


async def test_ensure_login_is_idempotent_until_forced(kratos_server):
    base, recorded = kratos_server
    auth = tadoku_auth.KratosAuth("e", "p", kratos_url=base)

    async with _session() as session:
        await auth.ensure_login(session)
        assert recorded["submits"] == 1
        await auth.ensure_login(session)  # already logged in -> no-op
        assert recorded["submits"] == 1
        await auth.ensure_login(session, force=True)  # refresh -> logs in again
        assert recorded["submits"] == 2


async def test_login_raises_on_non_200():
    async def browser(request):
        return web.json_response({"error": "boom"}, status=500)

    app = web.Application()
    app.router.add_get("/self-service/login/browser", browser)
    server = TestServer(app)
    await server.start_server()
    try:
        auth = tadoku_auth.KratosAuth("e", "p", kratos_url=str(server.make_url("")).rstrip("/"))
        async with _session() as session:
            with pytest.raises(tadoku_auth.TadokuAuthError):
                await auth.ensure_login(session)
    finally:
        await server.close()
