"""Slice 3 tests: LLM client JSON handling + distiller (pass 1)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.distiller import distill
from app.services.llm_client import LLMClient, parse_json


class TestParseJson:
    def test_plain(self):
        assert parse_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_embedded(self):
        assert parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_json("")


class TestLLMClient:
    @respx.mock
    def test_complete_json_parses_and_returns_usage(self):
        respx.post("http://localhost:11434/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": '```json\n{"summary":"x","key_topics":[],"segments":[]}\n```'}}],
                "usage": {"total_tokens": 42},
            })
        )
        client = LLMClient(base_url="http://localhost:11434/v1", model="test-model")
        data, usage = client.complete_json("sys", "user")
        assert data["summary"] == "x"
        assert usage["total_tokens"] == 42
        # Character accounting: prompt = len('sys')+len('user'); completion = raw content len.
        assert usage["prompt_chars"] == 7
        assert usage["completion_chars"] == len(
            '```json\n{"summary":"x","key_topics":[],"segments":[]}\n```'
        )


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete_json(self, system, user, json_schema=None):
        self.calls += 1
        return self.responses.pop(0), {
            "total_tokens": 10, "prompt_chars": len(system) + len(user), "completion_chars": 5,
        }


class TestDistiller:
    def test_single_pass(self):
        llm = FakeLLM([{"summary": "buy AAPL", "key_topics": ["apple"], "segments": []}])
        out, usage = distill(llm, "short transcript")
        assert out.summary == "buy AAPL"
        assert llm.calls == 1
        assert usage["total_tokens"] == 10
        assert usage["completion_chars"] == 5

    def test_map_reduce_for_long_text(self):
        llm = FakeLLM([
            {"summary": "part1", "key_topics": [], "segments": []},
            {"summary": "part2", "key_topics": [], "segments": []},
            {"summary": "part3", "key_topics": [], "segments": []},
            {"summary": "combined", "key_topics": ["x"], "segments": []},
        ])
        out, usage = distill(llm, "x" * 25, max_chunk_chars=10)
        assert out.summary == "combined"   # reduce output
        assert llm.calls == 4              # 3 chunks + 1 reduce
        assert usage["total_tokens"] == 40
        # completion_chars summed across all 4 calls (5 each).
        assert usage["completion_chars"] == 20

    def test_recovers_when_summary_wrapped_under_title_key(self):
        # phi4-style deviation: whole object nested under a single title key.
        llm = FakeLLM([{
            "CNBC Fast Money Transcript Summary (July 9, 2026)": {
                "summary": "buy NVDA", "key_topics": ["nvidia"], "segments": [],
            }
        }])
        out, _ = distill(llm, "short transcript")
        assert out.summary == "buy NVDA"
        assert out.key_topics == ["nvidia"]

    def test_recovers_from_alternate_markdown_key(self):
        llm = FakeLLM([{"markdown": "**Summary**\n- point", "key_topics": [], "segments": []}])
        out, _ = distill(llm, "short transcript")
        assert out.summary == "**Summary**\n- point"
