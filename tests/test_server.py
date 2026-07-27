"""End-to-end tests of the HTTP surface against a stubbed ChatGPT backend."""

from __future__ import annotations

import json
from typing import Any
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from chatgpt_proxy.auth import AuthError
from chatgpt_proxy.config import Settings
from chatgpt_proxy.server import create_app
from chatgpt_proxy.upstream import UpstreamError

TOOL_CALL_EVENTS = [
    {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "DocumentClassifierSchema"},
    },
    {"type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": '{"title":'},
    {"type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": '"Invoice"}'},
    {
        "type": "response.completed",
        "response": {
            "model": "gpt-5.6-luna",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "DocumentClassifierSchema",
                    "arguments": '{"title":"Invoice"}',
                },
            ],
            "usage": {"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
        },
    },
]


class FakeBackend:
    """Stands in for CodexBackend; records the payload it was handed."""

    def __init__(self, events: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.events = events or []
        self.error = error
        self.last_payload: dict[str, Any] | None = None

    async def stream_responses(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.last_payload = payload
        if self.error:
            raise self.error
        for event in self.events:
            yield event

    async def list_models(self) -> list[dict[str, Any]]:
        return [{"slug": "gpt-5.1"}, {"slug": "gpt-5.6-luna"}]

    async def aclose(self) -> None:
        return None


def make_client(tmp_path, backend: FakeBackend, *, api_key: str | None = None) -> TestClient:
    settings = Settings(
        token_file=tmp_path / "auth.json",
        host="127.0.0.1",
        port=8080,
        proxy_api_key=api_key,
        default_model="gpt-5.6-luna",
        reasoning_effort="low",
        request_timeout=30,
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    client.app.state.backend = backend
    return client


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(TOOL_CALL_EVENTS)


@pytest.fixture
def client(tmp_path, backend) -> TestClient:
    client = make_client(tmp_path, backend)
    yield client
    client.__exit__(None, None, None)


class TestChatCompletions:
    def test_tool_call_completion(self, client, backend):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.6-luna",
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "classify this"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "DocumentClassifierSchema", "parameters": {"type": "object"}},
                    },
                ],
                "tool_choice": "required",
            },
        )

        assert response.status_code == 200
        body = response.json()
        call = body["choices"][0]["message"]["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"title": "Invoice"}
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert body["usage"]["prompt_tokens"] == 120

        # The request reached the backend already translated.
        assert backend.last_payload["instructions"] == "system prompt"
        assert backend.last_payload["stream"] is True
        assert backend.last_payload["tools"][0]["name"] == "DocumentClassifierSchema"

    def test_streaming_emits_sse_chunks(self, tmp_path):
        events = [
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]
        client = make_client(tmp_path, FakeBackend(events))
        try:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                payloads = [
                    line[6:]
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]
        finally:
            client.__exit__(None, None, None)

        assert payloads[-1] == "[DONE]"
        text = "".join(
            json.loads(payload)["choices"][0]["delta"].get("content", "")
            for payload in payloads[:-1]
        )
        assert text == "Hello"

    def test_stream_without_completion_is_an_error(self, tmp_path):
        client = make_client(tmp_path, FakeBackend([{"type": "response.output_text.delta", "delta": "x"}]))
        try:
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 502
        assert "without completing" in response.json()["error"]["message"]

    def test_invalid_json_body(self, client):
        response = client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"


class TestErrorMapping:
    def test_missing_login_maps_to_401(self, tmp_path):
        client = make_client(tmp_path, FakeBackend(error=AuthError("not signed in")))
        try:
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "chatgpt_login_required"

    def test_quota_exhaustion_maps_to_429(self, tmp_path):
        error = UpstreamError("rate limited", status_code=429, retry_after="600")
        client = make_client(tmp_path, FakeBackend(error=error))
        try:
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 429
        assert response.headers["retry-after"] == "600"
        assert response.json()["error"]["type"] == "rate_limit_error"


class TestAccessControl:
    def test_requests_without_key_are_rejected(self, tmp_path, backend):
        client = make_client(tmp_path, backend, api_key="s3cret")
        try:
            unauthorised = client.post("/v1/chat/completions", json={"messages": []})
            authorised = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer s3cret"},
            )
        finally:
            client.__exit__(None, None, None)

        assert unauthorised.status_code == 401
        assert authorised.status_code == 200

    def test_status_page_requires_key(self, tmp_path, backend):
        client = make_client(tmp_path, backend, api_key="s3cret")
        try:
            unauthorised = client.get("/")
            authorised = client.get(
                "/",
                headers={"Authorization": "Bearer s3cret"},
            )
        finally:
            client.__exit__(None, None, None)

        assert unauthorised.status_code == 401
        assert authorised.status_code == 200
        assert "paperless-chatgpt-proxy" in authorised.text

    def test_health_stays_open(self, tmp_path, backend):
        client = make_client(tmp_path, backend, api_key="s3cret")
        try:
            response = client.get("/healthz")
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 200
        assert response.json() == {"ok": True, "logged_in": False}


class TestModels:
    def test_models_are_returned_in_openai_shape(self, client):
        response = client.get("/v1/models")

        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert [model["id"] for model in body["data"]] == ["gpt-5.1", "gpt-5.6-luna"]
