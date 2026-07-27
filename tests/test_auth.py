"""Tests for token storage and the refresh path.

Refresh tokens rotate and are single-use, so the persistence behaviour here is
what stands between a working proxy and a login that silently dies overnight.
"""

from __future__ import annotations

import base64
import json
import stat
import time

import httpx
import pytest

from chatgpt_proxy import config
from chatgpt_proxy.auth import AuthError
from chatgpt_proxy.auth import AuthManager
from chatgpt_proxy.auth import DeviceCode
from chatgpt_proxy.auth import TokenStore
from chatgpt_proxy.auth import credentials_from_tokens
from chatgpt_proxy.auth import exchange_code_for_tokens
from chatgpt_proxy.auth import import_codex_auth
from chatgpt_proxy.auth import poll_device_code
from chatgpt_proxy.auth import request_device_code
from chatgpt_proxy.auth import tokens_from_response


def jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def id_token(account_id: str = "acct_1", email: str = "user@example.com", plan: str = "plus") -> str:
    return jwt(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan,
            },
        },
    )


def access_token(expires_in: int) -> str:
    return jwt({"exp": int(time.time()) + expires_in})


class TestTokenStore:
    def test_round_trip(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": "a"})

        assert store.read() == {"access_token": "a"}

    def test_file_is_not_world_readable(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": "a"})

        mode = stat.S_IMODE((tmp_path / "auth.json").stat().st_mode)
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0

    def test_missing_file_reads_as_none(self, tmp_path):
        assert TokenStore(tmp_path / "nope.json").read() is None

    def test_corrupt_file_is_reported(self, tmp_path):
        path = tmp_path / "auth.json"
        path.write_text("{ broken")

        with pytest.raises(AuthError, match="not valid JSON"):
            TokenStore(path).read()

    def test_clear_is_idempotent(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.clear()
        store.write({"access_token": "a"})
        store.clear()

        assert store.read() is None


class TestTokenParsing:
    def test_nested_claims_are_extracted(self):
        tokens = tokens_from_response(
            {"id_token": id_token(), "access_token": "at", "refresh_token": "rt"},
        )

        assert tokens["account_id"] == "acct_1"
        assert tokens["email"] == "user@example.com"
        assert tokens["plan_type"] == "plus"

    def test_missing_refresh_token_falls_back_to_previous(self):
        previous = {"refresh_token": "old", "id_token": id_token()}

        tokens = tokens_from_response({"access_token": "at"}, previous=previous)

        assert tokens["refresh_token"] == "old"
        assert tokens["account_id"] == "acct_1"

    def test_response_without_access_token_is_rejected(self):
        with pytest.raises(AuthError, match="no access_token"):
            tokens_from_response({"id_token": id_token()})

    def test_expiry_comes_from_the_access_token(self):
        creds = credentials_from_tokens({"access_token": access_token(3600)})

        assert 3590 < creds.expires_in <= 3600


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestOAuthErrorRedaction:
    private_body = "oauth-private-response-content-123"

    @pytest.mark.asyncio
    async def test_device_code_error_does_not_expose_response_body(self):
        async with mock_client(
            lambda _request: httpx.Response(500, text=self.private_body),
        ) as client:
            with pytest.raises(AuthError) as excinfo:
                await request_device_code(client)

        assert "HTTP 500" in str(excinfo.value)
        assert self.private_body not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_device_poll_error_does_not_expose_response_body(self):
        device_code = DeviceCode(
            verification_url="https://example.invalid/device",
            user_code="TEST-CODE",
            device_auth_id="device-1",
            interval=1,
        )
        async with mock_client(
            lambda _request: httpx.Response(500, text=self.private_body),
        ) as client:
            with pytest.raises(AuthError) as excinfo:
                await poll_device_code(client, device_code)

        assert "HTTP 500" in str(excinfo.value)
        assert self.private_body not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_token_exchange_error_does_not_expose_response_body(self):
        async with mock_client(
            lambda _request: httpx.Response(500, text=self.private_body),
        ) as client:
            with pytest.raises(AuthError) as excinfo:
                await exchange_code_for_tokens(
                    client,
                    code="authorization-code",
                    code_verifier="verifier",
                )

        assert "HTTP 500" in str(excinfo.value)
        assert self.private_body not in str(excinfo.value)


class TestAuthManager:
    @pytest.mark.asyncio
    async def test_valid_token_is_used_as_is(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": access_token(3600), "refresh_token": "rt", "account_id": "acct_1"})

        def handler(request):  # pragma: no cover - must not be called
            raise AssertionError("no refresh expected")

        async with mock_client(handler) as client:
            creds = await AuthManager(store).credentials(client)

        assert creds.account_id == "acct_1"

    @pytest.mark.asyncio
    async def test_expiring_token_is_refreshed_and_rotation_persisted(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": access_token(60), "refresh_token": "rt_old", "account_id": "acct_1"})
        seen = {}

        def handler(request):
            assert str(request.url) == config.OAUTH_TOKEN_URL
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id_token": id_token(),
                    "access_token": access_token(3600),
                    "refresh_token": "rt_new",
                },
            )

        async with mock_client(handler) as client:
            creds = await AuthManager(store).credentials(client)

        assert seen == {
            "client_id": config.CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": "rt_old",
        }
        assert creds.expires_in > config.REFRESH_MARGIN_SECONDS
        # The rotated token must land on disk, or the next refresh is rejected.
        assert store.read()["refresh_token"] == "rt_new"

    @pytest.mark.asyncio
    async def test_reused_refresh_token_gives_an_actionable_error(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": access_token(0), "refresh_token": "rt"})

        def handler(request):
            return httpx.Response(400, json={"error": "refresh_token_reused"})

        async with mock_client(handler) as client:
            with pytest.raises(AuthError, match="already used"):
                await AuthManager(store).credentials(client)

    @pytest.mark.asyncio
    async def test_no_login_is_reported_clearly(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")

        async with mock_client(lambda request: httpx.Response(200)) as client:
            with pytest.raises(AuthError, match="not signed in"):
                await AuthManager(store).credentials(client)

    @pytest.mark.asyncio
    async def test_force_refresh_ignores_a_still_valid_expiry(self, tmp_path):
        store = TokenStore(tmp_path / "auth.json")
        store.write({"access_token": access_token(3600), "refresh_token": "rt"})
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={"id_token": id_token(), "access_token": access_token(3600), "refresh_token": "rt2"},
            )

        async with mock_client(handler) as client:
            await AuthManager(store).force_refresh(client)

        assert len(calls) == 1

    def test_status_without_login(self, tmp_path):
        assert AuthManager(TokenStore(tmp_path / "auth.json")).status() == {"logged_in": False}


class TestCodexImport:
    def test_import_reads_codex_auth_json(self, tmp_path):
        source = tmp_path / "codex-auth.json"
        source.write_text(
            json.dumps(
                {
                    "tokens": {
                        "id_token": {"raw_jwt": id_token(account_id="acct_9")},
                        "access_token": "at",
                        "refresh_token": "rt",
                    },
                },
            ),
        )
        store = TokenStore(tmp_path / "auth.json")

        tokens = import_codex_auth(source, store)

        assert tokens["account_id"] == "acct_9"
        assert store.read()["access_token"] == "at"

    def test_api_key_only_login_is_rejected(self, tmp_path):
        source = tmp_path / "codex-auth.json"
        source.write_text(json.dumps({"openai_api_key": "sk-test", "tokens": None}))

        with pytest.raises(AuthError, match="no ChatGPT tokens"):
            import_codex_auth(source, TokenStore(tmp_path / "auth.json"))

    def test_missing_file_is_reported(self, tmp_path):
        with pytest.raises(AuthError, match="does not exist"):
            import_codex_auth(tmp_path / "nope.json", TokenStore(tmp_path / "auth.json"))
