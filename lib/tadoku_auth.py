"""Keep a live tadoku.app login session (Ory Kratos) for the API client.

tadoku's internal API authenticates with an Ory Kratos session cookie
(``ory_kratos_session``) that expires. This module logs in with a bot account's
credentials via Kratos's password login flow, which drops that cookie into the
shared aiohttp cookie jar; every subsequent tadoku.app request then carries it
automatically. When a request later comes back 401/403 (the session lapsed), the
client asks this manager to log in again and retries -- so the id is refreshed
only when it's actually needed.

It's entirely opt-in: with no ``TADOKU_EMAIL`` / ``TADOKU_PASSWORD`` in the
environment, ``KratosAuth.from_env()`` returns ``None`` and the client stays
anonymous (which is how the public read endpoints have always been used).
"""

import asyncio
import logging
import os
from typing import Optional

import aiohttp

_log = logging.getLogger(__name__)

# Kratos public base for tadoku, from the frontend's config
# (NEXT_PUBLIC_KRATOS_ENDPOINT default). Overridable via env for flexibility.
DEFAULT_KRATOS_URL = "https://account.tadoku.app/kratos"

# Login is two quick calls; cap them so a slow Kratos can't wedge a poll.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class TadokuAuthError(Exception):
    """Raised when logging in to tadoku.app (Kratos) fails."""


def _find_csrf_token(nodes: list) -> Optional[str]:
    """Pull the ``csrf_token`` value out of a Kratos login flow's ui nodes."""
    for node in nodes or []:
        attrs = node.get("attributes") or {}
        if attrs.get("name") == "csrf_token":
            return attrs.get("value")
    return None


class KratosAuth:
    """Logs a bot account into tadoku.app and keeps the session cookie fresh.

    Holds no cookies itself -- they live in the aiohttp session's jar, shared with
    every API request -- just the credentials, plus a flag/lock so concurrent
    guild polls trigger at most one login.
    """

    def __init__(self, email: str, password: str, kratos_url: str = DEFAULT_KRATOS_URL) -> None:
        self.email = email
        self.password = password
        # Normalise away a trailing slash so URL joins are predictable.
        self.kratos_url = kratos_url.rstrip("/")
        self._logged_in = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> Optional["KratosAuth"]:
        """Build from ``TADOKU_EMAIL`` / ``TADOKU_PASSWORD`` (+ optional
        ``KRATOS_PUBLIC_URL``), or ``None`` when credentials aren't configured."""
        email = os.environ.get("TADOKU_EMAIL")
        password = os.environ.get("TADOKU_PASSWORD")
        if not email or not password:
            return None
        kratos_url = os.environ.get("KRATOS_PUBLIC_URL") or DEFAULT_KRATOS_URL
        return cls(email, password, kratos_url)

    async def ensure_login(self, session: aiohttp.ClientSession, *, force: bool = False) -> None:
        """Ensure a session cookie is present, logging in if needed.

        A no-op once logged in unless ``force`` (used after a 401/403 to refresh a
        lapsed session). The lock means a burst of callers logs in once, not once
        each.
        """
        if self._logged_in and not force:
            return
        async with self._lock:
            # Re-check under the lock: another caller may have just logged in.
            if self._logged_in and not force:
                return
            await self._login(session)
            self._logged_in = True

    async def _login(self, session: aiohttp.ClientSession) -> None:
        """Run the Kratos password login flow, leaving ``ory_kratos_session`` in
        the session's cookie jar."""
        # 1. Start a browser login flow; Kratos returns the flow (and a CSRF
        #    cookie in the jar). Accept JSON so it doesn't try to redirect us.
        try:
            async with session.get(
                f"{self.kratos_url}/self-service/login/browser",
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    raise TadokuAuthError(f"Kratos login-flow init returned {resp.status}")
                flow = await resp.json()

            action = (flow.get("ui") or {}).get("action")
            if not action:
                raise TadokuAuthError("Kratos login flow had no submit action")
            csrf = _find_csrf_token((flow.get("ui") or {}).get("nodes"))

            # 2. Submit credentials to the flow's action URL. On success Kratos
            #    sets the ``ory_kratos_session`` cookie (kept in the jar).
            body = {"method": "password", "identifier": self.email, "password": self.password}
            if csrf:
                body["csrf_token"] = csrf
            async with session.post(
                action, json=body, headers={"Accept": "application/json"}, timeout=_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    raise TadokuAuthError(f"Kratos login submit returned {resp.status}")
        except aiohttp.ClientError as e:
            raise TadokuAuthError(f"Could not reach Kratos: {e}") from e

        _log.info("Logged in to tadoku.app as %s", self.email)
