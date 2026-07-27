# paperless-chatgpt-proxy

An OpenAI-compatible API endpoint that answers from a **ChatGPT subscription**
instead of from prepaid API credits, so paperless-ngx's AI features (title/tag
suggestions and document chat) run on a plan you already pay for.

paperless-ngx itself is **not modified**. It already accepts any
OpenAI-compatible endpoint via `PAPERLESS_AI_LLM_BACKEND=openai-like`
(`src/paperless_ai/client.py`), so the whole integration is one environment
block and one extra container — and paperless stays upstream and updatable.

---

## Read this first

There is **no sanctioned way** for a third-party application to bill model
usage to a ChatGPT plan. The only credential that draws on a subscription is
the OAuth token issued to the Codex CLI, and it is accepted only at the
undocumented endpoint `chatgpt.com/backend-api/codex/responses`.

This proxy reproduces that flow from the Codex CLI's Apache-2.0 sources. That
means:

- **It is outside OpenAI's terms of use.** Using it risks rate limiting or
  account action. Only you can decide whether that is acceptable.
- **It can break without warning.** Anthropic blocked equivalent third-party
  use in February 2026 and started billing it as overage in April 2026; Google
  did the same for the Gemini CLI in February 2026. OpenAI has not, *yet*.
- **It is bound by your plan's quota window**, not by a credit balance. When
  the window is exhausted you get HTTP 429 until it resets, and paperless will
  show a failed suggestion rather than a partial one.

If none of that is acceptable, use `PAPERLESS_AI_LLM_BACKEND=ollama` with a
local model instead — no keys, no quota, no documents leaving the house.

---

## What works

| paperless feature | Status |
| --- | --- |
| Title / tag / correspondent suggestions | Yes — needs function calling, which the proxy translates |
| Document chat (`paperless_ai/chat.py`) | Yes — streamed token by token |
| RAG over similar documents | Yes, but embeddings must run locally (see below) |
| Embeddings from OpenAI | **No** — the ChatGPT backend serves no embedding models |

Set `PAPERLESS_AI_LLM_EMBEDDING_BACKEND=huggingface` so the vector index is
built locally. `openai-like` embeddings would still require a real API key and
real credits.

---

## Setup

### 1. Build and start the proxy

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # put in PROXY_API_KEY
docker compose up -d --build
```

### 2. Sign in with ChatGPT

Device-code login — no browser needed on the server:

```bash
docker compose exec chatgpt-proxy chatgpt-proxy login
```

It prints a URL and a one-time code. Open the URL on any device, sign in, enter
the code. Tokens land in the `chatgpt-proxy-data` volume and are refreshed
automatically from then on.

```bash
docker compose exec chatgpt-proxy chatgpt-proxy status
```

Already signed in to a Codex CLI on the same host? `chatgpt-proxy import-codex`
adopts that login — but see *Refresh tokens rotate* below.

### 3. Point paperless-ngx at it

Add to your paperless `webserver` service (and put it on the same Docker
network as the proxy):

```yaml
environment:
  PAPERLESS_AI_ENABLED: "true"
  PAPERLESS_AI_LLM_BACKEND: openai-like
  PAPERLESS_AI_LLM_ENDPOINT: http://chatgpt-proxy:8080/v1
  PAPERLESS_AI_LLM_API_KEY: ${PROXY_API_KEY}
  PAPERLESS_AI_LLM_MODEL: gpt-5.1-codex-mini
  PAPERLESS_AI_LLM_REQUEST_TIMEOUT: "300"
  PAPERLESS_AI_LLM_EMBEDDING_BACKEND: huggingface
  PAPERLESS_AI_LLM_EMBEDDING_MODEL: sentence-transformers/all-MiniLM-L6-v2
```

A complete stack is in [`examples/paperless-compose.yml`](examples/paperless-compose.yml).

### 4. Check it end to end

```bash
curl -s http://localhost:8080/v1/models -H "Authorization: Bearer $PROXY_API_KEY" | jq .
```

Then trigger a suggestion on any document in paperless.

---

## How it works

```
paperless-ngx (llama-index OpenAILike)
        │  POST /v1/chat/completions   (function calling, optionally streamed)
        ▼
chatgpt-proxy
        │  translate → Responses API request
        │  attach Authorization: Bearer <ChatGPT access token>
        │          ChatGPT-Account-ID: <workspace>
        ▼
https://chatgpt.com/backend-api/codex/responses   ← billed to your plan
        │  SSE event stream
        ▼
chatgpt-proxy  → folded back into a Chat Completions reply
```

The backend only answers over SSE and never stores turns, so `stream: true`,
`store: false` and `include: ["reasoning.encrypted_content"]` are fixed in
every request. Non-streaming callers get the stream aggregated for them.

Chat parameters with no equivalent upstream (`temperature`, `top_p`, `n`,
`seed`, `stop`, …) are dropped rather than forwarded, because the backend
rejects the whole request otherwise. Set `CHATGPT_FORWARD_SAMPLING=1` to
forward `temperature`/`top_p` anyway.

### Refresh tokens rotate

Every refresh invalidates the previous refresh token. Two consequences:

- All reads and refreshes happen under an exclusive `flock` on the token file,
  so multiple workers or containers cannot race each other.
- **Do not share one login with a running Codex CLI.** After `import-codex`,
  whichever side refreshes first invalidates the other. Sign in separately, or
  accept re-authenticating in Codex.

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROXY_API_KEY` | *(none)* | Required key for `/v1/*`. Unset means unauthenticated — don't. |
| `PROXY_HOST` / `PROXY_PORT` | `0.0.0.0` / `8080` | Listen address |
| `CHATGPT_TOKEN_FILE` | `~/.config/chatgpt-proxy/auth.json` | Token store (`/data/auth.json` in Docker) |
| `CHATGPT_MODEL` | `gpt-5.1-codex-mini` | Model when the client sends none |
| `CHATGPT_REASONING_EFFORT` | `low` | `none`/`low`/`medium`/`high`; empty omits the field |
| `CHATGPT_REQUEST_TIMEOUT` | `300` | Upstream timeout in seconds |
| `CHATGPT_FORWARD_SAMPLING` | unset | Forward `temperature`/`top_p` upstream |

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat Completions, streaming and non-streaming, with tools |
| `POST` | `/v1/responses` | Responses API pass-through |
| `GET` | `/v1/models` | Models your plan can reach |
| `GET` | `/healthz` | Liveness, unauthenticated |
| `GET` | `/auth/status` | Account, plan, token expiry |
| `POST` | `/auth/login/start` | Start a device-code login over HTTP |
| `POST` | `/auth/logout` | Delete the stored tokens |

---

## Troubleshooting

**`401 chatgpt_login_required`** — not signed in, or the refresh chain broke.
Run `chatgpt-proxy login` again. If it keeps happening, a second client is
sharing the same tokens.

**`429 rate_limit_error`** — your plan's quota window is exhausted. The
`retry-after` header carries the wait. Lower `CHATGPT_REASONING_EFFORT` or pick
a smaller model to stretch the window.

**Suggestions time out** — raise `PAPERLESS_AI_LLM_REQUEST_TIMEOUT` *and*
`CHATGPT_REQUEST_TIMEOUT`; paperless defaults to 120s, which reasoning models
plus RAG context can exceed.

**`502` mentioning an unknown field** — the backend changed its request schema.
Compare `chatgpt_proxy/translate.py` against the current Codex sources.

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

66 tests cover the translation layer, the token store and refresh rotation, SSE
framing, the 401 retry, and the HTTP surface against a stubbed backend. None of
them touch the network.
