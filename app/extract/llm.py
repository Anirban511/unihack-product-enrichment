"""Groq client with a deliberately narrow contract.

Cost and hallucination are the same problem here, and the same design fixes
both: the model is never asked to *write* a product fact. It is asked to
*choose* - an index into a candidate list, or an evidence id it was handed.
Free text can be invented; an index into a list the caller controls cannot.

Every call is disk-cached on (model, messages) so re-running a catalogue, or a
demo, costs nothing after the first pass.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from diskcache import Cache

from app.config import settings

_cache = Cache(str(settings.cache_dir / "llm"), size_limit=512 * 1024 ** 2)
_lock = threading.Lock()
_client = None


@dataclass
class LlmBudget:
    """Per-item spend guard. When it runs out the pipeline degrades, not fails."""
    max_calls: int = 6
    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: List[str] = field(default_factory=list)

    def can_spend(self) -> bool:
        return self.calls < self.max_calls

    def as_dict(self) -> dict:
        return {
            "live_calls": self.calls, "cached_calls": self.cached_calls,
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "errors": self.errors,
        }


def _get_client():
    global _client
    with _lock:
        if _client is None:
            from groq import Groq
            key = settings.groq_api_key
            if not key:
                raise RuntimeError("GROQ_API_KEY is not configured")
            _client = Groq(api_key=key)
        return _client


def available() -> bool:
    return bool(settings.groq_api_key)


def _key(messages: List[dict], max_tokens: int, effort: str) -> str:
    blob = json.dumps([settings.groq_model, messages, max_tokens, effort],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _coerce_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def json_call(system: str, user: str, budget: LlmBudget,
              max_tokens: Optional[int] = None, effort: Optional[str] = None,
              retries: int = 2) -> Optional[dict]:
    """One constrained JSON turn. Returns None rather than raising."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    max_tokens = max_tokens or settings.groq_max_tokens
    effort = effort or settings.groq_reasoning_effort

    ck = _key(messages, max_tokens, effort)
    hit = _cache.get(ck)
    if hit is not None:
        budget.cached_calls += 1
        return hit

    if not available():
        budget.errors.append("groq api key missing")
        return None
    if not budget.can_spend():
        budget.errors.append("llm budget exhausted")
        return None

    last_err = ""
    for attempt in range(retries + 1):
        try:
            budget.calls += 1
            resp = _get_client().chat.completions.create(
                model=settings.groq_model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=settings.groq_temperature,
                max_tokens=max_tokens,
                reasoning_effort=effort,
            )
            usage = getattr(resp, "usage", None)
            if usage:
                budget.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                budget.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            data = _coerce_json(resp.choices[0].message.content)
            if data is not None:
                _cache.set(ck, data, expire=settings.cache_ttl_seconds)
                return data
            last_err = "unparseable json"
        except Exception as exc:
            last_err = "{}: {}".format(type(exc).__name__, str(exc)[:200])
            # A reasoning model that ran out of room needs headroom, not a retry.
            if "json_validate_failed" in last_err or "max completion tokens" in last_err:
                max_tokens = min(int(max_tokens * 1.8), 16000)
        if not budget.can_spend():
            break
    budget.errors.append(last_err or "llm call failed")
    return None


def stats() -> dict:
    return {"model": settings.groq_model, "cached_responses": len(_cache),
            "configured": available()}
