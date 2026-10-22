"""LLM layer: provider-agnostic text completion + strict JSON extraction.

* ``HTTPLLM`` talks to OpenAI-compatible, Anthropic, Gemini and Ollama endpoints
  over plain ``requests`` (no vendor SDK dependency).
* ``ScriptedLLM`` is a TEST DOUBLE. It replays canned replies so the controller's
  validation / fallback / logging can be tested deterministically. Anything that
  ran on it is labelled ``provider="scripted-test-double"`` in the logs and must
  never be presented as evidence that a real model behaves the same way.
* No call here ever includes secrets in exceptions or logs.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional, Protocol

import requests

from jobledger.logging_setup import get_logger

logger = get_logger("llm")


class LLMError(Exception):
    """kind: not_configured | auth | rate_limit | timeout | server | bad_response | network"""

    def __init__(self, kind: str, message: str, retry_after: Optional[float] = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retry_after = retry_after


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, json_mode: bool = True) -> str: ...


# ---------------------------------------------------------------------------
# JSON extraction (models wrap JSON in prose / code fences; be strict about the result)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> Any:
    """Return the first complete JSON object/array in ``text`` or raise ValueError."""
    if text is None:
        raise ValueError("empty model output")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S | re.I)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(s)):
                ch = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(s[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = s.find(opener, start + 1)
    raise ValueError("no valid JSON found in model output")


# ---------------------------------------------------------------------------
# Real provider
# ---------------------------------------------------------------------------

class HTTPLLM:
    def __init__(self, provider: str, model: str, api_key: str = "", base_url: str = "",
                 timeout: int = 60, max_retries: int = 2, session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.provider = provider.lower()
        self.model = model
        self._key = api_key
        self._base = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep
        if self.provider not in ("openai", "groq", "anthropic", "gemini", "ollama"):
            raise LLMError("not_configured", f"unsupported provider '{provider}'")
        if self.provider != "ollama" and not api_key:
            raise LLMError("not_configured", f"no API key for provider '{provider}'")

    # -- request shaping -----------------------------------------------------
    def _request(self, system: str, user: str, json_mode: bool):
        p = self.provider
        if p in ("openai", "groq"):
            url = (self._base or ("https://api.openai.com/v1" if p == "openai"
                                  else "https://api.groq.com/openai/v1")) + "/chat/completions"
            body = {"model": self.model, "temperature": 0,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            return url, {"Authorization": f"Bearer {self._key}"}, body
        if p == "anthropic":
            body = {"model": self.model, "max_tokens": 1500, "temperature": 0, "system": system,
                    "messages": [{"role": "user", "content": user}]}
            return ((self._base or "https://api.anthropic.com") + "/v1/messages",
                    {"x-api-key": self._key, "anthropic-version": "2023-06-01"}, body)
        if p == "gemini":
            url = (self._base or "https://generativelanguage.googleapis.com") + \
                f"/v1beta/models/{self.model}:generateContent"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0,
                                         **({"responseMimeType": "application/json"} if json_mode else {})}}
            return url, {"x-goog-api-key": self._key}, body
        # ollama
        body = {"model": self.model, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        return (self._base or "http://localhost:11434") + "/api/chat", {}, body

    def _parse(self, data: dict) -> str:
        p = self.provider
        try:
            if p in ("openai", "groq"):
                return data["choices"][0]["message"]["content"]
            if p == "anthropic":
                return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            if p == "gemini":
                return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
            return data["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("bad_response", f"unexpected response shape ({type(exc).__name__})")

    # -- call with bounded retries ------------------------------------------
    def complete(self, system: str, user: str, json_mode: bool = True) -> str:
        url, headers, body = self._request(system, user, json_mode)
        last: Optional[LLMError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(url, headers=headers, json=body, timeout=self.timeout)
            except requests.Timeout:
                last = LLMError("timeout", f"no response within {self.timeout}s")
            except requests.RequestException as exc:
                last = LLMError("network", type(exc).__name__)
            else:
                if resp.status_code == 200:
                    try:
                        return self._parse(resp.json())
                    except ValueError:
                        raise LLMError("bad_response", "non-JSON HTTP body")
                if resp.status_code in (401, 403):
                    raise LLMError("auth", f"HTTP {resp.status_code} (not retried)")
                if resp.status_code == 429:
                    ra = _retry_after(resp.headers.get("Retry-After"))
                    last = LLMError("rate_limit", "HTTP 429", retry_after=ra)
                elif resp.status_code >= 500:
                    last = LLMError("server", f"HTTP {resp.status_code}")
                else:
                    raise LLMError("bad_response", f"HTTP {resp.status_code} (not retried)")
            if attempt < self.max_retries:
                delay = min(last.retry_after if last.retry_after is not None else 2 ** attempt, 30)
                logger.warning("LLM call failed (%s); retry %d/%d in %.1fs", last.kind, attempt + 1,
                               self.max_retries, delay)
                self._sleep(delay)
        raise last  # type: ignore[misc]


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------

class ScriptedLLM:
    """Replays canned replies. ``replies`` items: str, Exception (raised), or callable(system, user)."""

    provider = "scripted-test-double"

    def __init__(self, replies=None, model: str = "scripted"):
        self.model = model
        self._replies = list(replies or [])
        self._default: Optional[Callable] = None
        self.calls: list[dict] = []

    def set_default(self, fn: Callable[[str, str], str]) -> "ScriptedLLM":
        self._default = fn
        return self

    def complete(self, system: str, user: str, json_mode: bool = True) -> str:
        self.calls.append({"system": system, "user": user})
        if self._replies:
            item = self._replies.pop(0)
        elif self._default is not None:
            item = self._default
        else:
            raise LLMError("bad_response", "ScriptedLLM has no more replies")
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(system, user)
        return item


def build_llm(settings) -> Optional[LLMClient]:
    """Real client from settings, or None when not configured (caller must say so)."""
    if (settings.llm_provider or "").lower() in ("", "none") or not settings.has_llm():
        return None
    return HTTPLLM(settings.llm_provider, settings.llm_model, api_key=settings.llm_api_key(),
                   base_url=settings.ollama_base_url if settings.llm_provider.lower() == "ollama" else "",
                   timeout=settings.llm_timeout, max_retries=settings.llm_max_retries)
