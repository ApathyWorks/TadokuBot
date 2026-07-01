"""Thin async wrapper over the public tadoku.app immersion API.

Confirmed live against production (no auth required for these GET
endpoints): https://tadoku.app/api/internal/immersion/
"""

import aiohttp

BASE_URL = "https://tadoku.app/api/internal/immersion"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class TadokuAPIError(Exception):
    """Raised for any non-2xx response or network failure talking to tadoku.app."""


def _stringify_query_value(value):
    # aiohttp/yarl only accept str/int/float query values -- bool isn't
    # allowed even though it's an int subclass, so it needs an explicit cast.
    return str(value).lower() if isinstance(value, bool) else value


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict:
    params = {
        k: _stringify_query_value(v)
        for k, v in (params or {}).items()
        if v is not None
    }
    try:
        async with session.get(f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise TadokuAPIError(f"tadoku.app returned {resp.status} for {path}")
            return await resp.json()
    except aiohttp.ClientError as e:
        raise TadokuAPIError(f"Could not reach tadoku.app: {e}") from e


async def list_contests(
    session: aiohttp.ClientSession,
    *,
    official: bool | None = None,
    page: int = 0,
    page_size: int = 25,
) -> list[dict]:
    data = await _get(
        session,
        "/contests",
        {"official": official, "page": page, "page_size": page_size},
    )
    return data.get("contests", [])


async def get_contest(session: aiohttp.ClientSession, contest_id: str) -> dict:
    return await _get(session, f"/contests/{contest_id}")


async def get_latest_official_contest(session: aiohttp.ClientSession) -> dict:
    return await _get(session, "/contests/latest-official")


async def get_contest_leaderboard(
    session: aiohttp.ClientSession,
    contest_id: str,
    *,
    page: int = 0,
    page_size: int = 15,
    language_code: str | None = None,
    activity_id: int | None = None,
) -> dict:
    return await _get(
        session,
        f"/contests/{contest_id}/leaderboard",
        {
            "page": page,
            "page_size": page_size,
            "language_code": language_code,
            "activity_id": activity_id,
        },
    )
