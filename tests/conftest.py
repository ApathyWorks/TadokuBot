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
        self.responses: dict[str, tuple[int, object]] = {}
        self.requests: list[tuple[str, dict]] = []

    def set_response(self, path: str, status: int, body: object) -> None:
        self.responses[path] = (status, body)

    async def _handler(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        status, body = self.responses.get(request.path, (404, {"error": "not found"}))
        return web.json_response(body, status=status)


@pytest.fixture
async def tadoku_server(monkeypatch):
    fake_api = FakeTadokuAPI()
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", fake_api._handler)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    monkeypatch.setattr(tadoku_client, "BASE_URL", str(client.make_url("")).rstrip("/"))

    try:
        yield fake_api
    finally:
        await client.close()
