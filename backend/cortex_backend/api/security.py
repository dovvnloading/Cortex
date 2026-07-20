"""Loopback request and launcher-session security for the preview API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from threading import RLock
from typing import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status


class SessionSecurityError(RuntimeError):
    """Raised for invalid or expired launcher-session credentials."""


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session_id: str
    expires_at: datetime


class SessionManager:
    """Issue one-time bootstrap exchanges and short-lived bearer sessions."""

    def __init__(
        self,
        *,
        bootstrap_token: str | None = None,
        ttl_seconds: int = 3600,
        allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost", "::1"),
    ):
        if ttl_seconds < 60:
            raise ValueError("session TTL must be at least 60 seconds")
        self._bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self._bootstrap_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._bootstrap_used = False
        self._ttl = timedelta(seconds=ttl_seconds)
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self._sessions: dict[str, SessionPrincipal] = {}
        self._lock = RLock()

    @property
    def bootstrap_token(self) -> str:
        """Return the launcher-only secret for the local exchange."""
        return self._bootstrap_token

    @property
    def bootstrap_expires_at(self) -> datetime:
        return self._bootstrap_expires_at

    def issue_bootstrap_token(self) -> tuple[str, datetime]:
        """Rotate the one-time browser handoff token for a running instance."""
        with self._lock:
            self._bootstrap_token = secrets.token_urlsafe(32)
            self._bootstrap_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            self._bootstrap_used = False
            return self._bootstrap_token, self._bootstrap_expires_at

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return self._allowed_hosts

    def exchange(self, bootstrap_token: str) -> SessionExchange:
        with self._lock:
            if self._bootstrap_used or self._bootstrap_expires_at <= datetime.now(timezone.utc) or not hmac.compare_digest(
                bootstrap_token,
                self._bootstrap_token,
            ):
                raise SessionSecurityError("invalid bootstrap token")
            self._bootstrap_used = True
            raw_token = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + self._ttl
            session_id = secrets.token_urlsafe(16)
            self._sessions[self._digest(raw_token)] = SessionPrincipal(
                session_id=session_id,
                expires_at=expires_at,
            )
            return SessionExchange(
                token=raw_token,
                principal=self._sessions[self._digest(raw_token)],
            )

    def authenticate(self, token: str) -> SessionPrincipal:
        now = datetime.now(timezone.utc)
        with self._lock:
            principal = self._sessions.get(self._digest(token))
            if principal is None or principal.expires_at <= now:
                raise SessionSecurityError("invalid or expired session")
            return principal

    def validate_request_context(self, request: Request) -> None:
        raw_host = request.headers.get("host") or ""
        host = (urlsplit(f"//{raw_host}").hostname or "").lower()
        if host not in self._allowed_hosts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid local host",
            )
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            origin_host = (parsed.hostname or "").lower()
            if parsed.scheme != "http" or origin_host not in self._allowed_hosts:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="invalid local origin",
                )

    def require(self, request: Request) -> SessionPrincipal:
        self.validate_request_context(request)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cortex session required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return self.authenticate(token)
        except SessionSecurityError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cortex session invalid or expired",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionExchange:
    token: str
    principal: SessionPrincipal
