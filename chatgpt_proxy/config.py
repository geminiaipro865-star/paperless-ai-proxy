"""Runtime configuration, all via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# OAuth client of the Codex CLI. Taken from the Apache-2.0 sources at
# github.com/openai/codex (codex-rs/login/src/auth/manager.rs).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_BASE = os.environ.get("CHATGPT_AUTH_BASE", "https://auth.openai.com").rstrip("/")
BACKEND_BASE = os.environ.get(
    "CHATGPT_BACKEND_BASE",
    "https://chatgpt.com/backend-api/codex",
).rstrip("/")

DEVICE_USERCODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
DEVICE_VERIFY_URL = f"{AUTH_BASE}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE}/deviceauth/callback"
OAUTH_TOKEN_URL = f"{AUTH_BASE}/oauth/token"

RESPONSES_URL = f"{BACKEND_BASE}/responses"
MODELS_URL = f"{BACKEND_BASE}/models"

# Codex identifies itself with these; the backend rejects unknown originators.
ORIGINATOR = "codex_cli_rs"
USER_AGENT = os.environ.get(
    "CHATGPT_USER_AGENT",
    "codex_cli_rs/0.0.0 (paperless-chatgpt-proxy)",
)

# Refresh the access token this long before it actually expires.
REFRESH_MARGIN_SECONDS = 5 * 60


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    token_file: Path
    host: str
    port: int
    proxy_api_key: str | None
    default_model: str
    reasoning_effort: str
    request_timeout: int
    forward_sampling: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            token_file=Path(
                os.environ.get("CHATGPT_TOKEN_FILE", "~/.config/chatgpt-proxy/auth.json"),
            ).expanduser(),
            host=os.environ.get("PROXY_HOST", "0.0.0.0"),
            port=_int_env("PROXY_PORT", 8080),
            proxy_api_key=os.environ.get("PROXY_API_KEY") or None,
            default_model=os.environ.get("CHATGPT_MODEL", "gpt-5.1-codex-mini"),
            reasoning_effort=os.environ.get("CHATGPT_REASONING_EFFORT", "low"),
            request_timeout=_int_env("CHATGPT_REQUEST_TIMEOUT", 300),
            forward_sampling=os.environ.get("CHATGPT_FORWARD_SAMPLING", "").lower()
            in ("1", "true", "yes"),
        )


def auth_headers_common() -> dict[str, str]:
    """Headers Codex attaches to every request, auth and API alike."""
    return {"originator": ORIGINATOR, "User-Agent": USER_AGENT}
