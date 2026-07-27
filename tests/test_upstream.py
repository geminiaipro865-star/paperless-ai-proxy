"""Tests for the ChatGPT backend client: SSE framing, headers, retry, errors."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from chatgpt_proxy import config
from chatgpt_proxy import upstream
from chatgpt_proxy.auth import AuthError
from chatgpt_proxy.auth import AuthManager
from chatgpt_proxy.auth import TokenStore
from chatgpt_proxy.upstream import CodexBackend
from chatgpt_proxy.upstream import UpstreamError


def jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def logged_in_store(tmp_path, *, expires_in: int = 3600) -> TokenStore:
    store = TokenStore(tmp_path / "auth.json")
    store.write(
        {
            "access_token": jwt({"exp": int(time.time()) + expires_in}),
            "refresh_token": "rt",
            "account_id": "acct_1",
        },
    )
    return store


def sse(*payloads: dict) -> bytes:
    return b"".join(f"data: {json.dumps(payload)}\n\n".encode() for payload in payloads)


def backend_with(tmp_path, handler) -> CodexBackend:
    backend = CodexBackend(AuthManager(logged_in_store(tmp_path)))
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return backend


class TestSseParsing:
    async def test_events_are_split_on_blank_lines(self):
        response = httpx.Response(
            200,
            content=sse({"type": "a"}, {"type": "b"}) + b"data: [DONE]\n\n",
        )

        events = [event async for event in upstream._iter_sse(response)]

        assert events == [{"type": "a"}, {"type": "b"}]

    async def test_comments_and_junk_are_skipped(self):
        response = httpx.Response(
            200,
            content=b": keep-alive\n\ndata: not-json\n\n" + sse({"type": "a"}),
        )

        events = [event async for event in upstream._iter_sse(response)]

        assert events == [{"type": "a"}]

    async def test_multiline_data_is_joined(self):
        response = httpx.Response(200, content=b'data: {"type":\ndata: "a"}\n\n')

        events = [event async for event in upstream._iter_sse(response)]

        assert events == [{"type": "a"}]

    async def test_trailing_event_without_blank_line(self):
        response = httpx.Response(200, content=b'data: {"type": "a"}')

        events = [event async for event in upstream._iter_sse(response)]

        assert events == [{"type": "a"}]


class TestRequestShape:
    async def test_auth_headers_match_what_the_backend_expects(self, tmp_path):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, content=sse({"type": "response.completed"}))

        backend = backend_with(tmp_path, handler)
        async for _ in backend.stream_responses({"model": "m"}):
            pass
        await backend.aclose()

        assert seen["url"] == config.RESPONSES_URL
        assert seen["headers"]["chatgpt-account-id"] == "acct_1"
        assert seen["headers"]["authorization"].startswith("Bearer ")
        assert seen["headers"]["originator"] == config.ORIGINATOR
        assert seen["headers"]["accept"] == "text/event-stream"


class TestErrorHandling:
    async def test_401_is_retried_once_after_a_refresh(self, tmp_path):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == config.OAUTH_TOKEN_URL:
                return httpx.Response(
                    200,
                    json={
                        "id_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_1"}}),
                        "access_token": jwt({"exp": int(time.time()) + 3600}),
                        "refresh_token": "rt2",
                    },
                )
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(401, content=b"expired")
            return httpx.Response(200, content=sse({"type": "response.completed"}))

        backend = backend_with(tmp_path, handler)
        events = [event async for event in backend.stream_responses({"model": "m"})]
        await backend.aclose()

        assert len(attempts) == 2
        assert events == [{"type": "response.completed"}]

    async def test_persistent_401_asks_for_a_new_login(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == config.OAUTH_TOKEN_URL:
                return httpx.Response(
                    200,
                    json={
                        "id_token": jwt({}),
                        "access_token": jwt({"exp": int(time.time()) + 3600}),
                        "refresh_token": "rt2",
                    },
                )
            return httpx.Response(401, content=b"nope")

        backend = backend_with(tmp_path, handler)
        with pytest.raises(AuthError, match="Sign in again"):
            async for _ in backend.stream_responses({"model": "m"}):
                pass
        await backend.aclose()

    async def test_429_carries_the_retry_after_header(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"quota", headers={"retry-after": "300"})

        backend = backend_with(tmp_path, handler)
        with pytest.raises(UpstreamError) as excinfo:
            async for _ in backend.stream_responses({"model": "m"}):
                pass
        await backend.aclose()

        assert excinfo.value.status_code == 429
        assert excinfo.value.retry_after == "300"
        assert "rate limit" in str(excinfo.value)

    async def test_server_errors_surface_as_502(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"boom")

        backend = backend_with(tmp_path, handler)
        with pytest.raises(UpstreamError) as excinfo:
            async for _ in backend.stream_responses({"model": "m"}):
                pass
        await backend.aclose()

        assert excinfo.value.status_code == 502


class TestModels:
    async def test_model_list_is_unwrapped(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{"slug": "gpt-5.1"}]})

        backend = backend_with(tmp_path, handler)
        models = await backend.list_models()
        await backend.aclose()

        assert models == [{"slug": "gpt-5.1"}]
