"""Persistent native Tadoku sessions, with coordinated renewal and login backoff."""

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from email.utils import parsedate_to_datetime

import aiohttp

_log = logging.getLogger(__name__)
IDENTITY_URL = "https://tadoku.app/api/internal/identity"
ORIGIN = "https://tadoku.app"
SESSION_FILE = Path(__file__).resolve().parent.parent / "data" / "tadoku-session.json"
COOKIE_NAME = "tadoku_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class TadokuAuthError(Exception):
    """A sanitized authentication/configuration error, safe to log."""


class TadokuAuth:
    """Keep a session independently of the shared HTTP session's cookie jar.

    Callers use the returned (generation, token) snapshot for one API request.
    A rejected generation only refreshes once, even with concurrent callers.
    Constructor URL/path overrides support isolated local-server tests.
    """

    def __init__(self, email: str, password: str, *, identity_url: str = IDENTITY_URL,
                 session_file: Path | None = None):
        self.email = email.strip()
        self.password = password
        self.identity_url = identity_url.rstrip("/")
        self.session_file = Path(session_file) if session_file is not None else SESSION_FILE
        self._token = None
        self._expires_at = 0.0
        self._retry_at = 0.0
        self._validated = False
        self._disabled = None
        self._failures = 0
        self._generation = 0
        self._lock = asyncio.Lock()
        self._restore()

    @classmethod
    def from_env(cls):
        if os.environ.get("KRATOS_PUBLIC_URL"):
            _log.warning("KRATOS_PUBLIC_URL is obsolete and ignored; remove it from configuration")
        email = os.environ.get("TADOKU_EMAIL", "").strip()
        password = os.environ.get("TADOKU_PASSWORD", "")
        if not email and not password:
            return None
        if not email or not password:
            raise TadokuAuthError("Set both TADOKU_EMAIL and TADOKU_PASSWORD, or neither for anonymous access")
        return cls(email, password)

    def _restore(self):
        try:
            state = json.loads(self.session_file.read_text())
            if not isinstance(state, dict) or state.get("email", "").casefold() != self.email.casefold():
                return
            expires = float(state.get("expires_at", 0))
            retry = float(state.get("retry_at", 0))
            if not math.isfinite(expires) or not math.isfinite(retry):
                raise ValueError
            token = state.get("token")
            if token is not None and (not isinstance(token, str) or not token or any(c in token for c in "\r\n")):
                raise ValueError
            self._retry_at = retry
            if expires > time.time() and token:
                self._token, self._expires_at = token, expires
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            _log.warning("Stored Tadoku session is unavailable or invalid; it will be replaced on login")

    def _persist(self):
        """Atomic mode-0600 JSON, never pickle or a serialized password."""
        temp = None
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=self.session_file.name + ".", suffix=".tmp",
                                        dir=self.session_file.parent)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"email": self.email, "token": self._token,
                           "expires_at": self._expires_at, "retry_at": self._retry_at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.session_file)
        except OSError:
            _log.warning("Could not persist Tadoku session; continuing with in-memory state")
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass
                except OSError:
                    _log.warning("Could not remove temporary Tadoku session file")

    def _defer(self, message: str, *, delay: float | None = None):
        if delay is None:
            delay = min(30 * 2 ** min(self._failures, 5), 900)
            self._failures += 1
        self._retry_at = time.time() + delay
        self._persist()
        raise TadokuAuthError(message) from None

    def _disable(self, message):
        self._disabled = message
        raise TadokuAuthError(message) from None

    async def ensure_login(self, session, *, rejected_generation: int | None = None):
        """Return a valid session snapshot; refresh a rejected snapshot once."""
        async with self._lock:
            if self._disabled:
                raise TadokuAuthError(self._disabled)
            if rejected_generation == self._generation:
                self._token = None
                self._validated = False
            if self._token and self._validated and self._expires_at > time.time():
                return self._generation, self._token
            if time.time() < self._retry_at:
                raise TadokuAuthError("Tadoku sign-in is paused; retry after the backoff period")
            if self._token and self._expires_at > time.time():
                if await self._validate(session):
                    self._validated = True
                    self._failures = 0
                    return self._generation, self._token
            self._token = None
            self._validated = False
            await self._login(session)
            return self._generation, self._token

    async def _validate(self, session):
        try:
            async with session.get(self.identity_url + "/session",
                                   cookies={COOKIE_NAME: self._token}, timeout=_TIMEOUT,
                                   allow_redirects=False) as response:
                if response.status == 401:
                    return False
                if response.status != 200:
                    self._defer("Tadoku session validation unavailable; no login attempted")
                body = await response.json()
            if not isinstance(body, dict) or "account" not in body:
                self._defer("Invalid Tadoku session response; no login attempted")
            account = body["account"]
            if account is None:
                return False
            if not isinstance(account, dict) or not account.get("id"):
                self._defer("Invalid Tadoku account response; no login attempted")
            return str(account.get("email", "")).casefold() == self.email.casefold()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            self._defer("Tadoku session validation failed; no login attempted")

    @staticmethod
    def _throttle_delay(header):
        try:
            delay = float(header)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(header).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 900
        return max(900, delay) if math.isfinite(delay) else 900

    async def _login(self, session):
        try:
            async with session.post(
                self.identity_url + "/login",
                json={"email": self.email, "password": self.password},
                headers={"Origin": ORIGIN}, timeout=_TIMEOUT, allow_redirects=False,
            ) as response:
                status = response.status
                if status == 429:
                    self._defer("Tadoku sign-in throttled; paused for at least 15 minutes",
                                delay=self._throttle_delay(response.headers.get("Retry-After")))
                if status == 401:
                    self._disable("Tadoku credentials rejected; correct configuration and restart")
                if status in (400, 403) or 300 <= status < 500:
                    self._disable(f"Tadoku login configuration rejected (HTTP {status}); correct it and restart")
                if status != 200:
                    self._defer(f"Tadoku sign-in unavailable (HTTP {status}); will retry later")
                body = await response.json()
                account = body.get("account") if isinstance(body, dict) else None
                cookie = response.cookies.get(COOKIE_NAME)
                if (not isinstance(account, dict) or not account.get("id") or
                        str(account.get("email", "")).casefold() != self.email.casefold() or
                        cookie is None or not cookie.value):
                    self._defer("Tadoku sign-in returned no valid account/session")
                # Use the server's Max-Age when supplied, capped at the documented lifetime.
                lifetime = min(int(cookie["max-age"] or SESSION_SECONDS), SESSION_SECONDS)
                if lifetime <= 0:
                    self._defer("Tadoku sign-in returned an expired session")
                self._token = cookie.value
                self._expires_at = time.time() + lifetime
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            self._defer("Could not complete Tadoku sign-in; will retry later")
        self._validated = True
        self._generation += 1
        self._failures = 0
        self._retry_at = 0
        self._persist()
        _log.info("Tadoku session established")
