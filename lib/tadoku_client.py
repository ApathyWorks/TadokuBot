"""Thin async wrapper over the tadoku.app immersion API.

Every function here is a small, focused coroutine that performs one GET
request and returns already-parsed JSON:

    https://tadoku.app/api/internal/immersion/

Authentication is optional and handled transparently. If a ``TadokuAuth`` is
installed via ``configure_auth`` (the bot does this at startup when tadoku
credentials are configured), every request carries the login session cookie and
a 401 triggers a re-login + one retry -- see ``lib.tadoku_auth``. With no
auth configured the client stays anonymous, as the public read endpoints have
always been used.

Keeping all HTTP knowledge in this one module means the cogs stay free of
URL-building and error-handling boilerplate: they call e.g.
``get_contest_leaderboard(...)`` and either get a dict back or catch a single
``TadokuAPIError``.
"""

from typing import Optional

import aiohttp

from lib.tadoku_auth import TadokuAuth, TadokuAuthError

# Root of every request. Individual functions append their endpoint path.
BASE_URL = "https://tadoku.app/api/internal/immersion"

# Cap every request so a hung/slow tadoku.app can't wedge a Discord
# interaction (Discord itself times out interactions after ~15 minutes, but
# users expect a leaderboard in seconds).
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Statuses that mean "your session lapsed": re-login once and retry.
_AUTH_STATUSES = (401,)

# The installed login-session manager, or None for anonymous access. Set once at
# startup via ``configure_auth``.
_auth: Optional[TadokuAuth] = None


def configure_auth(auth: Optional[TadokuAuth]) -> None:
    """Install (or clear) the native Tadoku session manager used by every request."""
    global _auth
    _auth = auth


class TadokuAPIError(Exception):
    """Raised for any non-200 response or network failure talking to tadoku.app.

    Cogs catch this single type to show a friendly "couldn't reach tadoku.app"
    message instead of leaking a raw traceback to the user. ``status`` is the
    HTTP status for a non-200 response, or ``None`` for a transport or login
    failure, so a caller can tell "not found" apart from an outage.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _stringify_query_value(value):
    """Coerce a query-parameter value into something aiohttp/yarl accepts.

    aiohttp's URL builder only allows str/int/float query values. ``bool`` is
    rejected even though it's technically an ``int`` subclass, so booleans need
    an explicit cast to the lowercase strings the API expects ("true"/"false").
    All other types are passed through untouched.
    """
    return str(value).lower() if isinstance(value, bool) else value


async def _fetch(session: aiohttp.ClientSession, url: str, params: dict, *, token: str | None = None) -> tuple[int, object]:
    """Do one GET; return ``(status, json_body_or_None)``.

    Only 200 responses carry a parsed body; other statuses return ``None`` so the
    caller can decide (retry after re-login, or raise). Transport errors collapse
    into ``TadokuAPIError``.
    """
    try:
        async with session.get(
            url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False,
            cookies={"tadoku_session": token} if token else {},
        ) as resp:
            if resp.status == 200:
                return 200, await resp.json()
            return resp.status, None
    except (aiohttp.ClientError, TimeoutError) as e:
        # Connection refused, DNS failure, timeout, etc. -- collapse the
        # aiohttp-specific exception into our single error type.
        raise TadokuAPIError(f"Could not reach tadoku.app: {e}") from e


async def _get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict:
    """Perform a GET against ``BASE_URL + path`` and return the parsed JSON body.

    Drops any params whose value is ``None`` (so callers can pass optional
    filters unconditionally) and normalises the rest via
    ``_stringify_query_value``. When authentication is configured the request
    carries the login session cookie, and a 401 (a lapsed session) triggers a
    re-login and a single retry. Any other non-200 status or transport error is
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
    url = f"{BASE_URL}{path}"

    generation = token = None
    # Make sure we're logged in before the first call (if auth is configured).
    if _auth is not None:
        try:
            generation, token = await _auth.ensure_login(session)
        except TadokuAuthError as e:
            raise TadokuAPIError(f"tadoku.app login failed: {e}") from e

    status, body = await _fetch(session, url, params, token=token)

    # A lapsed session comes back 401: refresh it once and retry.
    if status in _AUTH_STATUSES and _auth is not None:
        try:
            generation, token = await _auth.ensure_login(session, rejected_generation=generation)
        except TadokuAuthError as e:
            raise TadokuAPIError(f"tadoku.app login failed: {e}") from e
        status, body = await _fetch(session, url, params, token=token)

    # The API returns 200 on success and 404 for unknown contests/paths; treat
    # anything other than 200 as an error the caller must handle.
    if status != 200:
        raise TadokuAPIError(f"tadoku.app returned {status} for {path}", status=status)
    return body


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


async def list_user_logs(
    session: aiohttp.ClientSession,
    user_id: str,
    *,
    page: int = 0,
    page_size: int = 100,
) -> dict:
    """Fetch one page of a user's logs across *all* contests, newest first.

    Unlike ``list_contest_logs`` this is not scoped to a contest, so paging
    through it yields a user's entire immersion history -- what the log feed sums
    into lifetime characters/pages/listening for a claimed member's card. Each
    log carries ``amount``, ``unit_name``, optional ``duration_seconds``, and an
    ``activity``/``language`` object, plus ``deleted`` and ``created_at``.

    Returns the raw envelope ``{"logs": [...], "total_size": N}`` (unlike the
    contest-scoped helper, callers need ``total_size`` to know when to stop
    paging).
    """
    return await _get(
        session,
        f"/users/{user_id}/logs",
        {"page": page, "page_size": page_size},
    )


async def get_contest_events(
    session: aiohttp.ClientSession,
    contest_id: str,
    *,
    cursor: str,
    limit: int = 100,
) -> dict:
    """Fetch one page of a contest's log event feed, oldest first.

    ``cursor`` is an event id from a previous page, ``"now"`` (an empty page
    whose ``next_cursor`` is the latest event) or ``"0"`` (from the start).
    Returns ``{"events": [...], "next_cursor": id, "has_more": bool}``; each
    event has ``id``, ``log_id``, ``event`` (``log.submitted`` /
    ``log.updated`` / ``log.withdrawn``) and ``occurred_at``. A bad or
    wrong-contest cursor raises with ``status == 400``.
    """
    return await _get(
        session,
        f"/contests/{contest_id}/events",
        {"cursor": cursor, "limit": limit},
    )


async def get_contest_log(
    session: aiohttp.ClientSession, contest_id: str, log_id: str
) -> dict | None:
    """Fetch a log's current state as it appears in ``contest_id``.

    Same shape as an item of ``list_contest_logs``, with the score this
    contest gives it. Returns ``None`` when the log isn't in the contest (any
    more): withdrawn, deleted, or removed by a moderator. Every other failure
    still raises, so an outage or a permission error never looks like a
    withdrawal.
    """
    try:
        return await _get(session, f"/contests/{contest_id}/logs/{log_id}")
    except TadokuAPIError as e:
        if e.status == 404:
            return None
        raise
