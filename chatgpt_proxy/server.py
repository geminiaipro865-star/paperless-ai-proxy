"""FastAPI app exposing an OpenAI-compatible API backed by a ChatGPT plan."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from typing import AsyncIterator

from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse

from . import translate
from .auth import AuthError
from .auth import AuthManager
from .auth import TokenStore
from .auth import run_device_login
from .config import Settings
from .upstream import CodexBackend
from .upstream import UpstreamError

logger = logging.getLogger("chatgpt_proxy.server")


def _error_body(message: str, *, kind: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = TokenStore(settings.token_file)
    auth = AuthManager(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.backend = CodexBackend(auth, timeout=settings.request_timeout)
        app.state.login_state = {"status": "idle"}
        app.state.login_task = None
        try:
            yield
        finally:
            await app.state.backend.aclose()

    app = FastAPI(title="paperless-chatgpt-proxy", lifespan=lifespan)
    app.state.settings = settings
    app.state.auth = auth

    # ------------------------------------------------------------------
    # Access control for this proxy itself
    # ------------------------------------------------------------------

    def require_api_key(request: Request) -> None:
        expected = settings.proxy_api_key
        if not expected:
            return
        header = request.headers.get("authorization", "")
        presented = (
            header[7:].strip()
            if header[:7].lower() == "bearer "
            else request.headers.get("x-api-key", "")
        )
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(
                status_code=401,
                detail=_error_body("invalid proxy API key", kind="invalid_request_error"),
            )

    guard = [Depends(require_api_key)]

    # ------------------------------------------------------------------
    # Error handling: answer in the OpenAI error shape so clients can parse it
    # ------------------------------------------------------------------

    @app.exception_handler(AuthError)
    async def _auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=_error_body(str(exc), kind="authentication_error", code="chatgpt_login_required"),
        )

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_request: Request, exc: UpstreamError) -> JSONResponse:
        headers = {"retry-after": exc.retry_after} if exc.retry_after else None
        kind = "rate_limit_error" if exc.status_code == 429 else "api_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(str(exc), kind=kind),
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        content = detail if isinstance(detail, dict) else _error_body(str(detail), kind="invalid_request_error")
        return JSONResponse(status_code=exc.status_code, content=content)

    # ------------------------------------------------------------------
    # Health and auth
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        status = auth.status()
        return {"ok": True, "logged_in": status["logged_in"]}

    @app.get("/auth/status", dependencies=guard)
    async def auth_status() -> dict[str, Any]:
        return auth.status()

    @app.post("/auth/login/start", dependencies=guard)
    async def auth_login_start() -> dict[str, Any]:
        task = app.state.login_task
        if task is not None and not task.done():
            return app.state.login_state

        prompt: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

        def on_prompt(url: str, code: str) -> None:
            if not prompt.done():
                prompt.set_result({"verification_url": url, "user_code": code})

        async def run() -> None:
            try:
                await run_device_login(store, on_prompt=on_prompt)
                app.state.login_state = {"status": "complete", **auth.status()}
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
                logger.exception("device login failed")
                app.state.login_state = {"status": "error", "detail": str(exc)}
                if not prompt.done():
                    prompt.set_exception(exc)

        app.state.login_task = asyncio.create_task(run())
        try:
            details = await asyncio.wait_for(asyncio.shield(prompt), timeout=30)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=_error_body("auth service did not issue a device code", kind="api_error"),
            ) from None
        except Exception as exc:  # noqa: BLE001 - login failed before a code was issued
            raise HTTPException(
                status_code=502,
                detail=_error_body(f"device code login failed: {exc}", kind="api_error"),
            ) from exc
        app.state.login_state = {"status": "pending", **details}
        return app.state.login_state

    @app.get("/auth/login/status", dependencies=guard)
    async def auth_login_status() -> dict[str, Any]:
        return app.state.login_state

    @app.post("/auth/logout", dependencies=guard)
    async def auth_logout() -> dict[str, Any]:
        store.clear()
        app.state.login_state = {"status": "idle"}
        return {"logged_out": True}

    @app.get("/", dependencies=guard, response_class=HTMLResponse)
    async def index() -> str:
        status = auth.status()
        if status["logged_in"]:
            body = (
                f"<p>Signed in as <strong>{status.get('email') or 'unknown'}</strong> "
                f"(plan: {status.get('plan_type') or 'unknown'}).</p>"
                f"<p>Access token expires in {status.get('access_token_expires_in')}s "
                "and is refreshed automatically.</p>"
            )
        else:
            body = (
                "<p>Not signed in. Run <code>docker compose exec chatgpt-proxy "
                "python -m chatgpt_proxy login</code> "
                "or POST to <code>/auth/login/start</code>.</p>"
            )
        return (
            "<html><head><title>paperless-chatgpt-proxy</title></head>"
            "<body style='font-family:system-ui;max-width:40rem;margin:3rem auto'>"
            "<h1>paperless-chatgpt-proxy</h1>" + body + "</body></html>"
        )

    # ------------------------------------------------------------------
    # OpenAI-compatible surface
    # ------------------------------------------------------------------

    @app.get("/v1/models", dependencies=guard)
    async def list_models() -> dict[str, Any]:
        models = await app.state.backend.list_models()
        now = int(time.time())
        data = []
        for model in models:
            slug = (model.get("slug") or model.get("id")) if isinstance(model, dict) else str(model)
            if not slug:
                continue
            data.append({"id": slug, "object": "model", "created": now, "owned_by": "openai"})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions", dependencies=guard)
    async def chat_completions(request: Request) -> Any:
        body = await _json_body(request)
        payload = translate.chat_to_responses(
            body,
            default_model=settings.default_model,
            reasoning_effort=settings.reasoning_effort,
            forward_sampling=settings.forward_sampling,
        )
        dropped = translate.dropped_parameters(body)
        if dropped:
            logger.debug("ignoring unsupported chat parameters: %s", ", ".join(dropped))

        model = payload["model"]
        translator = translate.StreamTranslator(model=model)
        backend = app.state.backend

        if body.get("stream"):

            async def event_stream() -> AsyncIterator[bytes]:
                async for event in backend.stream_responses(payload):
                    for chunk in translator.handle(event):
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                if translator.error:
                    error = _error_body(translator.error, kind="api_error")
                    yield f"data: {json.dumps(error)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        chunks: list[dict[str, Any]] = []
        async for event in backend.stream_responses(payload):
            chunks.extend(translator.handle(event))

        if translator.error:
            raise UpstreamError(translator.error, status_code=502)
        if translator.final_response is None:
            raise UpstreamError("ChatGPT backend closed the stream without completing the response")

        return translate.responses_to_chat_completion(
            translator.final_response,
            model=model,
            completion_id=translator.completion_id,
            created=translator.created,
        )

    @app.post("/v1/responses", dependencies=guard)
    async def responses(request: Request) -> Any:
        """Pass-through for clients that already speak the Responses API."""
        body = await _json_body(request)
        wants_stream = bool(body.get("stream", False))
        payload = {**body, "stream": True, "store": False}
        backend = app.state.backend

        if wants_stream:

            async def event_stream() -> AsyncIterator[bytes]:
                async for event in backend.stream_responses(payload):
                    yield f"data: {json.dumps(event)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        final: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        async for event in backend.stream_responses(payload):
            if event.get("type") == "response.completed":
                final = event.get("response")
            elif event.get("type") in ("response.failed", "error"):
                error = event
        if final is None:
            message = json.dumps(error)[:500] if error else "stream ended without response.completed"
            raise UpstreamError(f"ChatGPT backend did not return a response: {message}")
        return final

    return app


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_body(f"invalid JSON body: {exc}", kind="invalid_request_error"),
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail=_error_body("request body must be a JSON object", kind="invalid_request_error"),
        )
    return body


app = create_app()
