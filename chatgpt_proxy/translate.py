"""Translation between the Chat Completions wire format and the Responses API.

paperless-ngx talks to us through llama-index's `OpenAILike`, which speaks
`/v1/chat/completions` including function calling. The ChatGPT backend only
speaks the Responses API and only in streaming mode, so every request is
translated on the way in and the SSE stream is folded back into a Chat
Completions reply on the way out.

Everything in this module is pure: no I/O, no globals -- so it can be tested
without a network.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from typing import Iterable

# Chat Completions parameters that have no equivalent on the codex backend and
# make it reject the request outright.
_DROPPED_SAMPLING_PARAMS = frozenset(
    {
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "n",
        "seed",
        "stop",
        "user",
    },
)


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _content_to_text(content: Any) -> str:
    """Flatten Chat Completions content (string or part list) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                chunks.append(part.get("text", ""))
        return "".join(chunks)
    return str(content)


def _content_parts(content: Any, *, text_type: str) -> list[dict[str, Any]]:
    """Convert content into Responses input/output parts, keeping images."""
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                parts.append({"type": text_type, "text": part})
            elif not isinstance(part, dict):
                continue
            elif part.get("type") in ("text", "input_text", "output_text"):
                parts.append({"type": text_type, "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if url:
                    parts.append({"type": "input_image", "image_url": url})
        return parts or [{"type": text_type, "text": ""}]
    return [{"type": text_type, "text": _content_to_text(content)}]


def _tools_to_responses(tools: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            # Pass through built-in tool declarations unchanged.
            converted.append(tool)
            continue
        fn = tool.get("function") or {}
        converted.append(
            {
                "type": "function",
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                "strict": bool(fn.get("strict", False)),
            },
        )
    return converted


def _tool_choice_to_responses(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function") or {}
        return {"type": "function", "name": fn.get("name", tool_choice.get("name", ""))}
    return tool_choice


def _response_format_to_text(response_format: Any) -> dict[str, Any] | None:
    if not isinstance(response_format, dict):
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return {"format": {"type": "json_object"}}
    if kind == "json_schema":
        schema = response_format.get("json_schema") or {}
        return {
            "format": {
                "type": "json_schema",
                "name": schema.get("name", "response"),
                "schema": schema.get("schema") or {},
                "strict": bool(schema.get("strict", False)),
            },
        }
    return None


def chat_to_responses(
    body: dict[str, Any],
    *,
    default_model: str,
    reasoning_effort: str = "low",
    forward_sampling: bool = False,
) -> dict[str, Any]:
    """Build a Responses API request from a Chat Completions request."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role in ("system", "developer"):
            text = _content_to_text(message.get("content"))
            if text:
                instructions.append(text)
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": _content_to_text(message.get("content")),
                },
            )
            continue

        if role == "assistant":
            text = _content_to_text(message.get("content"))
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    },
                )
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    },
                )
            continue

        # user, and anything else we do not recognise
        items.append(
            {
                "type": "message",
                "role": role or "user",
                "content": _content_parts(message.get("content"), text_type="input_text"),
            },
        )

    request: dict[str, Any] = {
        "model": body.get("model") or default_model,
        "instructions": "\n\n".join(instructions),
        "input": items,
        # The backend never persists these turns for us, and it only answers
        # over SSE -- both are fixed, not configurable.
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }

    if reasoning_effort:
        request["reasoning"] = {"effort": reasoning_effort}

    tools = body.get("tools")
    if tools:
        request["tools"] = _tools_to_responses(tools)
        request["tool_choice"] = _tool_choice_to_responses(body.get("tool_choice", "auto"))
        request["parallel_tool_calls"] = bool(body.get("parallel_tool_calls", True))

    text_param = _response_format_to_text(body.get("response_format"))
    if text_param:
        request["text"] = text_param

    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens")
    if max_tokens:
        request["max_output_tokens"] = max_tokens

    if forward_sampling:
        for key in ("temperature", "top_p"):
            if key in body:
                request[key] = body[key]

    return request


def dropped_parameters(body: dict[str, Any]) -> list[str]:
    """Chat parameters we silently ignore -- useful for a debug log line."""
    return sorted(key for key in body if key in _DROPPED_SAMPLING_PARAMS)


# --------------------------------------------------------------------------
# Responses -> Chat Completions
# --------------------------------------------------------------------------


def _usage_to_chat(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens", 0) or 0
    completion = usage.get("output_tokens", 0) or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("total_tokens", prompt + completion) or 0,
    }


def _finish_reason(response: dict[str, Any], *, has_tool_calls: bool) -> str:
    if response.get("status") == "incomplete":
        details = response.get("incomplete_details") or {}
        if details.get("reason") == "max_output_tokens":
            return "length"
    return "tool_calls" if has_tool_calls else "stop"


def responses_to_chat_completion(
    response: dict[str, Any],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    """Fold a finished Responses object into a Chat Completions reply."""
    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text_chunks.append(part.get("text", ""))
        elif kind == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "") or "{}",
                    },
                },
            )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_chunks) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    completion: dict[str, Any] = {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": response.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(response, has_tool_calls=bool(tool_calls)),
                "logprobs": None,
            },
        ],
    }
    usage = _usage_to_chat(response.get("usage"))
    if usage:
        completion["usage"] = usage
    return completion


class StreamTranslator:
    """Turns Responses SSE events into Chat Completions chunks.

    Feed it parsed SSE payloads with `handle()`; it yields zero or more chunk
    dicts per event. It also records the final response object so a caller can
    reuse it for the non-streaming path.
    """

    def __init__(self, *, model: str, completion_id: str | None = None, created: int | None = None) -> None:
        self.model = model
        self.completion_id = completion_id or new_completion_id()
        self.created = created or int(time.time())
        self.final_response: dict[str, Any] | None = None
        self.error: str | None = None
        self._role_sent = False
        self._tool_index: dict[str, int] = {}

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        if delta and not self._role_sent:
            delta = {"role": "assistant", **delta}
            self._role_sent = True
        return {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}],
        }

    def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")

        if kind == "response.output_text.delta":
            delta = event.get("delta") or ""
            return [self._chunk({"content": delta})] if delta else []

        if kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return []
            item_id = item.get("id") or item.get("call_id") or ""
            index = len(self._tool_index)
            self._tool_index[item_id] = index
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": item.get("call_id") or item_id,
                                "type": "function",
                                "function": {"name": item.get("name", ""), "arguments": ""},
                            },
                        ],
                    },
                ),
            ]

        if kind == "response.function_call_arguments.delta":
            item_id = event.get("item_id", "")
            index = self._tool_index.setdefault(item_id, len(self._tool_index))
            delta = event.get("delta") or ""
            if not delta:
                return []
            return [
                self._chunk(
                    {"tool_calls": [{"index": index, "function": {"arguments": delta}}]},
                ),
            ]

        if kind == "response.completed":
            response = event.get("response") or {}
            self.final_response = response
            has_tool_calls = any(
                isinstance(item, dict) and item.get("type") == "function_call"
                for item in response.get("output") or []
            )
            final = self._chunk({}, finish_reason=_finish_reason(response, has_tool_calls=has_tool_calls))
            usage = _usage_to_chat(response.get("usage"))
            if usage:
                final["usage"] = usage
            return [final]

        if kind in ("response.failed", "error"):
            payload = event.get("response") or event
            error = payload.get("error") or {}
            self.error = error.get("message") or json.dumps(payload)[:500]
            return []

        return []
