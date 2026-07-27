"""Tests for the Chat Completions <-> Responses translation.

The request shapes mirror what paperless-ngx actually sends through
llama-index's `OpenAILike`: a system prompt, one user turn, and a single
required function tool (`paperless_ai/client.py`).
"""

from __future__ import annotations

import json

import pytest

from chatgpt_proxy import translate

CLASSIFIER_TOOL = {
    "type": "function",
    "function": {
        "name": "DocumentClassifierSchema",
        "description": "Extract document metadata",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
}

PAPERLESS_REQUEST = {
    "model": "gpt-5.1-codex-mini",
    "messages": [
        {"role": "system", "content": "You are an AI assistant integrated into Paperless-ngx."},
        {"role": "user", "content": "Analyze the following document...\nFilename: invoice.pdf"},
    ],
    "tools": [CLASSIFIER_TOOL],
    "tool_choice": "required",
    "temperature": 0.1,
    "stream": False,
}


class TestChatToResponses:
    def test_system_messages_become_instructions(self):
        payload = translate.chat_to_responses(PAPERLESS_REQUEST, default_model="fallback")

        assert payload["instructions"] == "You are an AI assistant integrated into Paperless-ngx."
        assert [item["role"] for item in payload["input"]] == ["user"]
        assert payload["input"][0]["content"][0]["type"] == "input_text"

    def test_fixed_backend_requirements(self):
        payload = translate.chat_to_responses(PAPERLESS_REQUEST, default_model="fallback")

        # The codex backend only answers over SSE and never stores turns for us.
        assert payload["stream"] is True
        assert payload["store"] is False
        assert payload["include"] == ["reasoning.encrypted_content"]

    def test_tools_are_flattened(self):
        payload = translate.chat_to_responses(PAPERLESS_REQUEST, default_model="fallback")

        tool = payload["tools"][0]
        assert tool == {
            "type": "function",
            "name": "DocumentClassifierSchema",
            "description": "Extract document metadata",
            "parameters": CLASSIFIER_TOOL["function"]["parameters"],
            "strict": False,
        }
        assert payload["tool_choice"] == "required"

    def test_named_tool_choice_is_unwrapped(self):
        body = {**PAPERLESS_REQUEST, "tool_choice": {"type": "function", "function": {"name": "x"}}}

        payload = translate.chat_to_responses(body, default_model="fallback")

        assert payload["tool_choice"] == {"type": "function", "name": "x"}

    def test_sampling_params_are_dropped_by_default(self):
        payload = translate.chat_to_responses(PAPERLESS_REQUEST, default_model="fallback")

        assert "temperature" not in payload
        assert translate.dropped_parameters(PAPERLESS_REQUEST) == ["temperature"]

    def test_sampling_params_can_be_forwarded(self):
        payload = translate.chat_to_responses(
            PAPERLESS_REQUEST,
            default_model="fallback",
            forward_sampling=True,
        )

        assert payload["temperature"] == 0.1

    def test_model_falls_back_when_unset(self):
        payload = translate.chat_to_responses(
            {"messages": [{"role": "user", "content": "hi"}]},
            default_model="gpt-5.1",
        )

        assert payload["model"] == "gpt-5.1"

    def test_reasoning_effort_can_be_disabled(self):
        payload = translate.chat_to_responses(
            PAPERLESS_REQUEST,
            default_model="fallback",
            reasoning_effort="",
        )

        assert "reasoning" not in payload

    def test_assistant_tool_call_round_trip(self):
        body = {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"a"}'},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ],
        }

        payload = translate.chat_to_responses(body, default_model="m")

        assert payload["input"][1] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"q":"a"}',
        }
        assert payload["input"][2] == {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        }

    def test_multimodal_user_content(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                },
            ],
        }

        payload = translate.chat_to_responses(body, default_model="m")

        assert payload["input"][0]["content"] == [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
        ]

    def test_json_schema_response_format(self):
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": {"type": "object"}, "strict": True},
            },
        }

        payload = translate.chat_to_responses(body, default_model="m")

        assert payload["text"] == {
            "format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}, "strict": True},
        }

    def test_max_tokens_is_renamed(self):
        payload = translate.chat_to_responses(
            {"messages": [], "max_tokens": 256},
            default_model="m",
        )

        assert payload["max_output_tokens"] == 256


class TestResponsesToChatCompletion:
    def test_text_response(self):
        response = {
            "model": "gpt-5.1",
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
        }

        completion = translate.responses_to_chat_completion(response, model="gpt-5.1")

        choice = completion["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": "Hello"}
        assert choice["finish_reason"] == "stop"
        assert completion["usage"] == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}

    def test_tool_call_response_is_what_paperless_parses(self):
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "DocumentClassifierSchema",
                    "arguments": '{"title":"Invoice","tags":["bill"]}',
                },
            ],
        }

        completion = translate.responses_to_chat_completion(response, model="m")

        choice = completion["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        call = choice["message"]["tool_calls"][0]
        assert call["id"] == "call_abc"
        assert call["function"]["name"] == "DocumentClassifierSchema"
        assert json.loads(call["function"]["arguments"])["title"] == "Invoice"

    def test_truncated_response_reports_length(self):
        response = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "par"}]}],
        }

        completion = translate.responses_to_chat_completion(response, model="m")

        assert completion["choices"][0]["finish_reason"] == "length"

    def test_empty_output_yields_null_content(self):
        completion = translate.responses_to_chat_completion({"output": []}, model="m")

        assert completion["choices"][0]["message"]["content"] is None


class TestStreamTranslator:
    def _deltas(self, chunks):
        return [chunk["choices"][0]["delta"] for chunk in chunks]

    def test_text_deltas(self):
        translator = translate.StreamTranslator(model="m")

        first = translator.handle({"type": "response.output_text.delta", "delta": "He"})
        second = translator.handle({"type": "response.output_text.delta", "delta": "llo"})

        assert self._deltas(first) == [{"role": "assistant", "content": "He"}]
        assert self._deltas(second) == [{"content": "llo"}]

    def test_role_is_sent_once(self):
        translator = translate.StreamTranslator(model="m")
        chunks = []
        for delta in ("a", "b", "c"):
            chunks.extend(translator.handle({"type": "response.output_text.delta", "delta": delta}))

        roles = [chunk["choices"][0]["delta"].get("role") for chunk in chunks]
        assert roles == ["assistant", None, None]

    def test_empty_delta_produces_no_chunk(self):
        translator = translate.StreamTranslator(model="m")

        assert translator.handle({"type": "response.output_text.delta", "delta": ""}) == []

    def test_function_call_streaming(self):
        translator = translate.StreamTranslator(model="m")

        added = translator.handle(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "f"},
            },
        )
        args = translator.handle(
            {"type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": '{"a":'},
        )

        assert self._deltas(added)[0]["tool_calls"][0]["function"]["name"] == "f"
        assert self._deltas(added)[0]["tool_calls"][0]["index"] == 0
        assert self._deltas(args)[0]["tool_calls"][0] == {
            "index": 0,
            "function": {"arguments": '{"a":'},
        }

    def test_parallel_function_calls_get_distinct_indices(self):
        translator = translate.StreamTranslator(model="m")

        for item_id in ("item_1", "item_2"):
            translator.handle(
                {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "id": item_id, "call_id": item_id, "name": "f"},
                },
            )
        second = translator.handle(
            {"type": "response.function_call_arguments.delta", "item_id": "item_2", "delta": "{}"},
        )

        assert self._deltas(second)[0]["tool_calls"][0]["index"] == 1

    def test_completed_event_finishes_and_records_response(self):
        translator = translate.StreamTranslator(model="m")
        response = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }

        chunks = translator.handle({"type": "response.completed", "response": response})

        assert chunks[0]["choices"][0]["finish_reason"] == "stop"
        assert chunks[0]["usage"]["total_tokens"] == 3
        assert translator.final_response is response

    def test_failure_is_captured(self):
        translator = translate.StreamTranslator(model="m")

        chunks = translator.handle(
            {"type": "response.failed", "response": {"error": {"message": "quota exceeded"}}},
        )

        assert chunks == []
        assert translator.error == "quota exceeded"

    def test_unknown_events_are_ignored(self):
        translator = translate.StreamTranslator(model="m")

        assert translator.handle({"type": "response.reasoning_summary_text.delta", "delta": "x"}) == []


class TestJwtDecoding:
    def test_claims_are_read_without_verification(self):
        from chatgpt_proxy.auth import decode_jwt_claims

        # header.payload.signature with an unpadded base64url payload
        import base64

        payload = base64.urlsafe_b64encode(json.dumps({"exp": 42, "email": "a@b.c"}).encode())
        token = "x." + payload.decode().rstrip("=") + ".y"

        assert decode_jwt_claims(token) == {"exp": 42, "email": "a@b.c"}

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.!!!.c", "a.aGk=.c"])
    def test_malformed_tokens_yield_no_claims(self, token):
        from chatgpt_proxy.auth import decode_jwt_claims

        assert decode_jwt_claims(token) == {}
