"""ChatGPT OAuth: device-code login, token storage, token refresh.

The flow mirrors the Codex CLI (Apache-2.0, github.com/openai/codex,
`codex-rs/login/src/device_code_auth.rs`), because the ChatGPT backend only
accepts tokens issued to that OAuth client.

Refresh tokens are single-use and rotate on every refresh -- the auth service
answers a reused one with `refresh_token_reused` and invalidates the chain. All
reads and refreshes therefore happen under an exclusive file lock so that
neither multiple workers nor a second container can race each other.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Iterator

import httpx

from . import config


class AuthError(RuntimeError):
    """Login is missing, expired or was revoked -- the user must sign in again."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    account_id: str | None
    email: str | None
    plan_type: str | None
    expires_at: int | None

    @property
    def expires_in(self) -> int | None:
        if self.expires_at is None:
            return None
        return self.expires_at - int(time.time())


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it.

    Verification is the auth service's job -- we only read the claims to learn
    the account id and the expiry, and a forged token would simply be rejected
    upstream.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload)
    except (binascii.Error, ValueError):
        return {}
    try:
        claims = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _auth_claims(id_token: str) -> dict[str, Any]:
    claims = decode_jwt_claims(id_token)
    nested = claims.get("https://api.openai.com/auth")
    return nested if isinstance(nested, dict) else {}


class TokenStore:
    """auth.json on disk, guarded by an flock so refreshes serialise."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self) -> Iterator["TokenStore"]:
        self._ensure_parent()
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> dict[str, Any] | None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise AuthError(f"{self.path} is not valid JSON: {exc}") from exc
        return data if isinstance(data, dict) else None

    def write(self, data: dict[str, Any]) -> None:
        self._ensure_parent()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def tokens_from_response(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalise an OAuth token response into what we persist."""
    previous = previous or {}
    id_token = payload.get("id_token") or previous.get("id_token") or ""
    access_token = payload.get("access_token") or ""
    refresh_token = payload.get("refresh_token") or previous.get("refresh_token") or ""
    if not access_token:
        raise AuthError("auth service returned no access_token")

    claims = _auth_claims(id_token)
    return {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": claims.get("chatgpt_account_id") or previous.get("account_id"),
        "email": decode_jwt_claims(id_token).get("email") or previous.get("email"),
        "plan_type": claims.get("chatgpt_plan_type") or previous.get("plan_type"),
        "last_refresh": int(time.time()),
    }


def credentials_from_tokens(tokens: dict[str, Any]) -> Credentials:
    access_token = tokens.get("access_token") or ""
    exp = decode_jwt_claims(access_token).get("exp")
    return Credentials(
        access_token=access_token,
        account_id=tokens.get("account_id"),
        email=tokens.get("email"),
        plan_type=tokens.get("plan_type"),
        expires_at=int(exp) if isinstance(exp, (int, float)) else None,
    )


# --------------------------------------------------------------------------
# Device code login
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCode:
    verification_url: str
    user_code: str
    device_auth_id: str
    interval: int


async def request_device_code(client: httpx.AsyncClient) -> DeviceCode:
    response = await client.post(
        config.DEVICE_USERCODE_URL,
        json={"client_id": config.CLIENT_ID},
        headers={"Content-Type": "application/json", **config.auth_headers_common()},
    )
    if response.status_code == 404:
        raise AuthError(
            "device code login is not available on this auth server "
            "(HTTP 404 from /deviceauth/usercode)",
        )
    if response.status_code >= 400:
        raise AuthError(f"device code request failed: HTTP {response.status_code}")

    body = response.json()
    interval = body.get("interval", 5)
    try:
        interval = int(str(interval).strip())
    except ValueError:
        interval = 5
    return DeviceCode(
        verification_url=config.DEVICE_VERIFY_URL,
        user_code=body.get("user_code") or body.get("usercode", ""),
        device_auth_id=body["device_auth_id"],
        interval=max(interval, 1),
    )


async def poll_device_code(
    client: httpx.AsyncClient,
    device_code: DeviceCode,
    *,
    max_wait_seconds: int = 15 * 60,
) -> dict[str, Any]:
    """Poll until the user approved the code; returns the PKCE + auth code."""
    deadline = time.monotonic() + max_wait_seconds
    while True:
        response = await client.post(
            config.DEVICE_TOKEN_URL,
            json={
                "device_auth_id": device_code.device_auth_id,
                "user_code": device_code.user_code,
            },
            headers={"Content-Type": "application/json", **config.auth_headers_common()},
        )
        if response.status_code < 400:
            return response.json()
        # 403/404 mean "not approved yet" in this flow.
        if response.status_code in (403, 404):
            if time.monotonic() >= deadline:
                raise AuthError("device authorisation timed out after 15 minutes")
            await asyncio.sleep(min(device_code.interval, max(deadline - time.monotonic(), 1)))
            continue
        raise AuthError(f"device authorisation failed: HTTP {response.status_code}")


async def exchange_code_for_tokens(
    client: httpx.AsyncClient,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str = config.DEVICE_REDIRECT_URI,
) -> dict[str, Any]:
    response = await client.post(
        config.OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.CLIENT_ID,
            "code_verifier": code_verifier,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            **config.auth_headers_common(),
        },
    )
    if response.status_code >= 400:
        raise AuthError(f"token exchange failed: HTTP {response.status_code}")
    return response.json()


async def refresh_tokens(client: httpx.AsyncClient, refresh_token: str) -> dict[str, Any]:
    response = await client.post(
        config.OAUTH_TOKEN_URL,
        json={
            "client_id": config.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json", **config.auth_headers_common()},
    )
    if response.status_code >= 400:
        body = response.text
        hint = ""
        for code, message in (
            ("refresh_token_expired", "the refresh token expired"),
            ("refresh_token_reused", "the refresh token was already used -- is a Codex CLI sharing this auth.json?"),
            ("refresh_token_invalidated", "the refresh token was revoked"),
        ):
            if code in body:
                hint = f" ({message})"
                break
        raise AuthError(f"token refresh failed: HTTP {response.status_code}{hint}. Sign in again.")
    return response.json()


class AuthManager:
    """Serves a valid access token, refreshing shortly before expiry."""

    def __init__(self, store: TokenStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        tokens = self.store.read()
        if not tokens:
            return {"logged_in": False}
        creds = credentials_from_tokens(tokens)
        return {
            "logged_in": True,
            "email": creds.email,
            "plan_type": creds.plan_type,
            "account_id": creds.account_id,
            "access_token_expires_in": creds.expires_in,
        }

    async def credentials(self, client: httpx.AsyncClient) -> Credentials:
        tokens = self.store.read()
        if not tokens or not tokens.get("access_token"):
            raise AuthError("not signed in -- run `chatgpt-proxy login`")

        creds = credentials_from_tokens(tokens)
        if not self._needs_refresh(creds):
            return creds

        async with self._lock:
            # Re-read under the file lock: another process may have rotated the
            # tokens while we waited, and reusing the old refresh token would
            # invalidate the whole chain.
            with self.store.locked():
                tokens = self.store.read() or {}
                creds = credentials_from_tokens(tokens)
                if not self._needs_refresh(creds):
                    return creds
                if not tokens.get("refresh_token"):
                    raise AuthError("access token expired and no refresh token is stored")

                payload = await refresh_tokens(client, tokens["refresh_token"])
                updated = tokens_from_response(payload, previous=tokens)
                self.store.write(updated)
                return credentials_from_tokens(updated)

    async def force_refresh(self, client: httpx.AsyncClient) -> Credentials:
        """Refresh regardless of the stored expiry.

        Used when the backend rejects a token that still looks valid to us --
        it can be revoked server-side before `exp` is reached.
        """
        async with self._lock:
            with self.store.locked():
                tokens = self.store.read() or {}
                if not tokens.get("refresh_token"):
                    raise AuthError("no refresh token stored -- sign in again")
                payload = await refresh_tokens(client, tokens["refresh_token"])
                updated = tokens_from_response(payload, previous=tokens)
                self.store.write(updated)
                return credentials_from_tokens(updated)

    @staticmethod
    def _needs_refresh(creds: Credentials) -> bool:
        if not creds.access_token:
            return True
        if creds.expires_at is None:
            return False
        return creds.expires_at - int(time.time()) <= config.REFRESH_MARGIN_SECONDS


async def run_device_login(store: TokenStore, *, on_prompt) -> dict[str, Any]:
    """Full device-code login; `on_prompt(url, code)` shows the code to the user."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        device_code = await request_device_code(client)
        on_prompt(device_code.verification_url, device_code.user_code)
        approved = await poll_device_code(client, device_code)
        payload = await exchange_code_for_tokens(
            client,
            code=approved["authorization_code"],
            code_verifier=approved["code_verifier"],
        )

    tokens = tokens_from_response(payload)
    with store.locked():
        store.write(tokens)
    return tokens


def import_codex_auth(codex_auth_path: Path, store: TokenStore) -> dict[str, Any]:
    """Adopt an existing `~/.codex/auth.json` login.

    Note that both sides then hold the same refresh token, and the first one to
    refresh invalidates the other's copy. Use this only if the Codex CLI on this
    machine is idle, or expect to sign in again there.
    """
    try:
        with codex_auth_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise AuthError(f"{codex_auth_path} does not exist") from exc

    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        raise AuthError(f"{codex_auth_path} contains no ChatGPT tokens (API-key login?)")

    id_token = tokens.get("id_token")
    # Codex stores id_token either as the raw JWT or as parsed claims.
    raw_jwt = id_token if isinstance(id_token, str) else (id_token or {}).get("raw_jwt", "")
    claims = _auth_claims(raw_jwt)
    imported = {
        "id_token": raw_jwt,
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "account_id": tokens.get("account_id") or claims.get("chatgpt_account_id"),
        "email": decode_jwt_claims(raw_jwt).get("email"),
        "plan_type": claims.get("chatgpt_plan_type"),
        "last_refresh": int(time.time()),
    }
    with store.locked():
        store.write(imported)
    return imported
