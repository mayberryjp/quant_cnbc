"""OpenAI-compatible LLM client with JSON/structured-output support.

Works against any OpenAI-compatible ``/chat/completions`` endpoint (Ollama,
LM Studio, vLLM, ...). The model is asked to return JSON; callers validate the
result against a Pydantic schema.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("quant_cnbc.llm")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json(content: str) -> dict[str, Any]:
    """Best-effort parse of a model's textual JSON response."""
    content = (content or "").strip()
    if not content:
        raise ValueError("empty LLM response")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = _FENCE.search(content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = content.find("{"), content.rfind("}")
    if 0 <= start < end:
        return json.loads(content[start : end + 1])
    raise ValueError("could not parse JSON from LLM response")


class LLMClient:
    def __init__(
        self, *, base_url: str, model: str, api_key: str = "", timeout: int = 120,
        max_tokens: int = 2048, json_mode: bool = True, num_ctx: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.json_mode = json_mode
        self.num_ctx = num_ctx
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    def complete_json(
        self, system: str, user: str, *, json_schema: dict | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (parsed_json, usage). Requests structured output when enabled."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if self.num_ctx is not None:
            # Ollama-specific: request a larger context window for this call.
            payload["options"] = {"num_ctx": self.num_ctx}
        if json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        elif self.json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = self._client.post(f"{self.base_url}/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        usage = dict(body.get("usage", {}) or {})
        # Character accounting, summed alongside tokens across map/reduce calls.
        # prompt_chars = system + user actually sent; completion_chars = raw
        # model output before JSON parsing.
        usage["prompt_chars"] = len(system) + len(user)
        usage["completion_chars"] = len(content or "")
        log.info(
            "llm call: model=%s tokens=%s (prompt=%s, completion=%s) chars=%d/%d",
            self.model, usage.get("total_tokens", "?"),
            usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
            usage["prompt_chars"], usage["completion_chars"],
        )
        return parse_json(content), usage

    def close(self) -> None:
        self._client.close()
