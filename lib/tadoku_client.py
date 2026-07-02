"""Thin async wrapper over the public tadoku.app immersion API.

Every function here is a small, focused coroutine that performs one GET
request and returns already-parsed JSON. The bot never writes to tadoku.app
and never authenticates -- these endpoints are public read-only, confirmed
live against production:

    https://tadoku.app/api/internal/immersion/

Keeping all HTTP knowledge in this one module means the cogs stay free of
URL-building and error-handling boilerplate: they call e.g.
``get_contest_leaderboard(...)`` and either get a dict back or catch a single
``TadokuAPIError``.
"""

import aiohttp

# Root of every request. Individual functions append their endpoint path.
BASE_URL = "https://tadoku.app/api/internal/immersion"

# Cap every request so a hung/slow tadoku.app can't wedge a Discord
# interaction (Discord itself times out interactions after ~15 minutes, but
# users expect a leaderboard in seconds).
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class TadokuAPIError(Exception):
    """Raised for any non-200 response or network failure talking to tadoku.app.

    Cogs catch this single type to show a friendly "couldn't reach tadoku.app"
    message instead of leaking a raw traceback to the user.
    """


def _stringify_query_value(value):
    """Coerce a query-parameter value into something aiohttp/yarl accepts.

    aiohttp's URL builder only allows str/int/float query values. ``bool`` is
    rejected even though it's technically an ``int`` subclass, so booleans need
    an explicit cast to the lowercase strings the API expects ("true"/"false").
    All other types are passed through untouched.
    """
    return str(value).lower() if isinstance(value, bool) else value


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict:
    """Perform a GET against ``BASE_URL + path`` and return the parsed JSON body.

    Drops any params whose value is ``None`` (so callers can pass optional
    filters unconditionally) and normalises the rest via
    ``_stringify_query_value``. Any non-200 status or transport error is
    surfaced as a ``TadokuAPIError``.
    """
    # Build the final query string: skip unset (None) filters, stringify the
    # rest. This lets callers write {"language_code": maybe_none} without
    # branching on whether the filter was provided.
    params = {
        k: _stringify_query_value(v)
        for k, v in (params or {}).items()
        if v is not None
    }
    try:
        async with session.get(f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT) as resp:
            # The API returns 200 on success and 404 for unknown contests/paths;
            # treat anything other than 200 as an error the caller must handle.
            if resp.status != 200:
                raise TadokuAPIError(f"tadoku.app returned {resp.status} for {path}")
            return await resp.json()
    except aiohttp.ClientError as e:
        # Connection refused, DNS failure, timeout, etc. -- collapse the
        # aiohttp-specific exception into our single error type.
        raise TadokuAPIError(f"Could not reach tadoku.app: {e}") from e


async def list_contests(
    session: aiohttp.ClientSession,
    *,
    official: bool | None = None,
    page: int = 0,
    page_size: int = 25,
) -> list[dict]:
    """List contests, newest first, optionally filtered to official ones.

    Used by the ``/set_contest`` autocomplete so an admin can pick a contest by
    name. Returns the bare list of contest dicts (the API wraps them in a
    paginated envelope, which we unwrap here).
    """
    data = await _get(
        session,
        "/contests",
        {"official": official, "page": page, "page_size": page_size},
    )
    # ``contests`` is the list inside the pagination envelope; default to []
    # so callers never have to guard against a missing key.
    return data.get("contests", [])


async def get_contest(session: aiohttp.ClientSession, contest_id: str) -> dict:
    """Fetch a single contest's detail (title, dates, allowed languages, ...).

    Raises ``TadokuAPIError`` (from a 404) if the id doesn't exist, which is how
    ``/set_contest`` detects a stale/invalid selection.
    """
    return await _get(session, f"/contests/{contest_id}")


async def get_latest_official_contest(session: aiohttp.ClientSession) -> dict:
    """Fetch the current official contest.

    This is the fallback contest shown by ``/leaderboard`` in any server that
    hasn't pinned a specific contest via ``/set_contest``.
    """
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
    """Fetch one page of a contest's leaderboard.

    Returns the raw API dict, which contains an ``entries`` list (each with
    rank/user_display_name/score/is_tie) and a ``total_size`` count. The
    optional ``language_code`` and ``activity_id`` narrow the ranking to a
    single language or activity type (reading/listening); leaving them ``None``
    ranks across everything.
    """
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


async def list_contest_logs(
    session: aiohttp.ClientSession,
    contest_id: str,
    *,
    page: int = 0,
    page_size: int = 100,
) -> list[dict]:
    """Fetch one page of a contest's individual logs, newest first.

    Each log carries ``user_id``, ``user_display_name``, ``score``,
    ``created_at`` (an ISO-8601 UTC timestamp) and ``deleted``. The API returns
    logs in descending ``created_at`` order and pages backward in time, which is
    what lets ``/weeklyleaderboard`` stop early once it reaches logs older than
    its 7-day window. Returns the bare list (the API's pagination envelope is
    unwrapped here).
    """
    data = await _get(
        session,
        f"/contests/{contest_id}/logs",
        {"page": page, "page_size": page_size},
    )
    return data.get("logs", [])
