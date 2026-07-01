"""Shared pytest fixtures and test doubles for the whole suite.

Provides:
  * ``isolated_config_store`` -- redirects the JSON store to a temp file so
    tests never touch (or leak into) the real data/config.json.
  * ``fake_bot`` / ``make_interaction`` / ``interaction`` -- lightweight
    stand-ins that let cog callbacks be invoked directly, with no live Discord.
  * ``FakeTadokuAPI`` / ``tadoku_server`` -- a real loopback HTTP server used to
    exercise the API client without hitting the network.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import lib.config_store as config_store
import lib.tadoku_client as tadoku_client


@pytest.fixture(autouse=True)
def isolated_config_store(tmp_path, monkeypatch):
    """Every test gets its own config.json so tests can't see each other's
    state and never touch the real data/config.json."""
    monkeypatch.setattr(config_store, "_PATH", str(tmp_path / "config.json"))


@pytest.fixture
def fake_bot():
    """A stand-in for TadokuBot with just the attributes cogs actually use."""
    return SimpleNamespace(session=AsyncMock())


def make_interaction(*, guild_id=None, user_id=111):
    """Builds a fake discord.Interaction. response/followup are AsyncMocks so
    tests can assert on what was sent without a real Discord connection."""
    interaction = SimpleNamespace()
    interaction.guild_id = guild_id
    interaction.user = SimpleNamespace(id=user_id)
    interaction.command = SimpleNamespace(name="test-command")

    interaction.response = SimpleNamespace(
        defer=AsyncMock(),
        send_message=AsyncMock(),
        is_done=lambda: False,
    )
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


@pytest.fixture
def interaction():
    return make_interaction(guild_id=999)


class FakeTadokuAPI:
    """A real local aiohttp server standing in for tadoku.app.

    Using a real (loopback) HTTP server -- rather than a mocking library --
    means tests exercise the actual aiohttp request/response path with zero
    risk of a third-party mock library drifting out of sync with whatever
    aiohttp version discord.py pulls in.
    """

    def __init__(self):
        # Canned responses keyed by request path: path -> (status, json body).
        self.responses: dict[str, tuple[int, object]] = {}
        # Every received request, recorded so tests can assert on the exact path
        # and query string the client produced.
        self.requests: list[tuple[str, dict]] = []

    def set_response(self, path: str, status: int, body: object) -> None:
        """Register what to return for a given request path."""
        self.responses[path] = (status, body)

    async def _handler(self, request: web.Request) -> web.Response:
        """Single catch-all handler: record the request, reply with the canned
        response for its path (or a 404 if none was registered)."""
        self.requests.append((request.path, dict(request.query)))
        status, body = self.responses.get(request.path, (404, {"error": "not found"}))
        return web.json_response(body, status=status)


@pytest.fixture
async def tadoku_server(monkeypatch):
    """Spin up the fake API on a real loopback port and point the client at it.

    Yields the ``FakeTadokuAPI`` so a test can register responses and later
    inspect captured requests; tears the server down afterwards.
    """
    fake_api = FakeTadokuAPI()
    app = web.Application()
    # Route every GET path to the one handler; it dispatches by path internally.
    app.router.add_route("GET", "/{tail:.*}", fake_api._handler)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    # Redirect the client module's BASE_URL to this server's address so the real
    # request-building/parsing code runs unchanged against our fake.
    monkeypatch.setattr(tadoku_client, "BASE_URL", str(client.make_url("")).rstrip("/"))

    try:
        yield fake_api
    finally:
        await client.close()
