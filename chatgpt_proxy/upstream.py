"""HTTP client for the ChatGPT Codex backend."""

from __future__ import annotations

import json
import logging
from typing import Any
from typing import AsyncIterator

import httpx

from . import config
from .auth import AuthError
from .auth import AuthManager

logger = logging.getLogger("chatgpt_proxy.upstream")


class UpstreamError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class CodexBackend:
    def __init__(self, auth: AuthManager, *, timeout: int = 300) -> None:
        self.auth = auth
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _headers(self, *, accept: str, force_refresh: bool = False) -> dict[str, str]:
        creds = (
            await self.auth.force_refresh(self._client)
            if force_refresh
            else await self.auth.credentials(self._client)
        )
        headers = {
            "Authorization": f"Bearer {creds.access_token}",
            "Accept": accept,
            "Content-Type": "application/json",
            **config.auth_headers_common(),
        }
        if creds.account_id:
            headers["ChatGPT-Account-ID"] = creds.account_id
        return headers

    @staticmethod
    def _raise_for_status(status_code: int, body: str, retry_after: str | None) -> None:
        if status_code < 400:
            return
        if status_code == 401:
            raise AuthError("ChatGPT rejected the token (HTTP 401). Sign in again.")
        if status_code == 429:
            raise UpstreamError(
                "ChatGPT rate limit reached -- your plan's quota window is exhausted. "
                f"Upstream said: {body[:500]}",
                status_code=429,
                retry_after=retry_after,
            )
        raise UpstreamError(
            f"ChatGPT backend returned HTTP {status_code}: {body[:1000]}",
            status_code=502 if status_code >= 500 else status_code,
        )

    async def stream_responses(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST /responses and yield the parsed SSE events.

        A 401 is retried exactly once with a forcibly refreshed token: the
        stored access token can be revoked server-side before it expires.
        """
        for attempt in (0, 1):
            headers = await self._headers(accept="text/event-stream", force_refresh=attempt == 1)
            request = self._client.build_request(
                "POST",
                config.RESPONSES_URL,
                json=payload,
                headers=headers,
            )
            response = await self._client.send(request, stream=True)

            if response.status_code == 401 and attempt == 0:
                await response.aclose()
                logger.info("upstream returned 401, refreshing token and retrying once")
                continue

            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                retry_after = response.headers.get("retry-after")
                await response.aclose()
                self._raise_for_status(response.status_code, body, retry_after)

            try:
                async for event in _iter_sse(response):
                    yield event
            finally:
                await response.aclose()
            return

    async def list_models(self) -> list[dict[str, Any]]:
        for attempt in (0, 1):
            headers = await self._headers(accept="application/json", force_refresh=attempt == 1)
            response = await self._client.get(config.MODELS_URL, headers=headers)
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code >= 400:
                self._raise_for_status(
                    response.status_code,
                    response.text,
                    response.headers.get("retry-after"),
                )
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("models", "data"):
                    if isinstance(payload.get(key), list):
                        return payload[key]
            return payload if isinstance(payload, list) else []
        return []


async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON payloads from an SSE stream, ignoring comments and [DONE]."""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if buffer:
                payload = "\n".join(buffer)
                buffer.clear()
                event = _parse_sse_data(payload)
                if event is not None:
                    yield event
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        event = _parse_sse_data("\n".join(buffer))
        if event is not None:
            yield event


def _parse_sse_data(payload: str) -> dict[str, Any] | None:
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("ignoring non-JSON SSE payload (%d bytes)", len(payload))
        return None
    return parsed if isinstance(parsed, dict) else None
