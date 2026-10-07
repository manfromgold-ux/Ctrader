"""OpenRouter client: free models first, a paid model as fallback, and a hard monthly spending cap."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .state import DAY, State, month_start

API = "https://openrouter.ai/api/v1"
# Preference order when picking models automatically from OpenRouter's live catalogue.
FREE_PREFERENCES = [r"coder", r"qwen3", r"deepseek", r"gpt-oss", r"kimi", r"glm", r"llama-3\.3-70b", r"gemma"]
PAID_PREFERENCES = [r"qwen3-coder", r"deepseek-(chat|v3)", r"kimi-k2", r"gpt-oss-120b", r"glm-4", r"gemini-.*flash"]
FREE_MIN_INTERVAL_S = 3.2  # OpenRouter allows 20 requests/minute on free models


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    model: str
    cost_usd: float


class LLMClient(Protocol):
    def complete(self, system: str, user: str, *, purpose: str, max_tokens: int = 6000,
                 allow_paid: bool = True, prefer_paid: bool = False) -> Completion: ...


def _match_order(model_id: str, prefs: list[str]) -> int:
    for i, pattern in enumerate(prefs):
        if re.search(pattern, model_id):
            return i
    return len(prefs)


def pick_models(catalogue: list[dict], max_paid_price_per_mtok: float) -> tuple[list[str], str]:
    """Choose up to 4 free models and one paid model from OpenRouter's /models payload."""
    free, paid = [], []
    for m in catalogue:
        mid = m.get("id", "")
        ctx = int(m.get("context_length") or 0)
        if ctx < 32_000:
            continue
        pricing = m.get("pricing") or {}
        try:
            prompt_price = float(pricing.get("prompt", "1")) * 1e6
            completion_price = float(pricing.get("completion", "1")) * 1e6
        except (TypeError, ValueError):
            continue
        if mid.endswith(":free") or (prompt_price == 0 and completion_price == 0):
            free.append((_match_order(mid, FREE_PREFERENCES), -ctx, mid))
        elif completion_price <= max_paid_price_per_mtok and _match_order(mid, PAID_PREFERENCES) < len(PAID_PREFERENCES):
            paid.append((_match_order(mid, PAID_PREFERENCES), completion_price, mid))
    free.sort()
    paid.sort()
    return [m for _, _, m in free[:4]], (paid[0][2] if paid else "")


def extract_block(text: str, lang: str) -> str | None:
    """Return the body of the last ```lang fenced block (models sometimes draft one, then correct it)."""
    blocks = re.findall(rf"```{lang}[^\n]*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    return blocks[-1].strip() if blocks else None


def extract_json(text: str, lang: str = "json"):
    body = extract_block(text, lang)
    if body is None:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            return None
        body = text[start:]
    body = re.sub(r",\s*([}\]])", r"\1", body)  # tolerate trailing commas
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(body)
            return obj
        except json.JSONDecodeError:
            return None


class OpenRouter:
    def __init__(self, api_key: str, state: State, *, free_models: list[str] | None = None,
                 paid_model: str = "auto", max_paid_price_per_mtok: float = 3.0, monthly_budget_usd: float = 15.0,
                 app_url: str = "https://github.com/reef-fleet", client: httpx.Client | None = None):
        self.state = state
        self.monthly_budget_usd = monthly_budget_usd
        self.max_paid_price_per_mtok = max_paid_price_per_mtok
        self._free = free_models or []
        self._paid = "" if paid_model == "auto" else paid_model
        self._auto_paid = paid_model == "auto"
        self._last_free_call = 0.0
        self._http = client or httpx.Client(
            base_url=API, timeout=httpx.Timeout(240, connect=20),
            headers={"Authorization": f"Bearer {api_key}", "HTTP-Referer": app_url, "X-Title": "Reef"},
        )

    # ---- model selection -------------------------------------------------------------------
    def models(self) -> tuple[list[str], str]:
        if self._free and (self._paid or not self._auto_paid):
            return self._free, self._paid
        cached = self.state.get("openrouter_models")
        if not cached or time.time() - cached["ts"] > DAY:
            resp = self._http.get("/models")
            resp.raise_for_status()
            free, paid = pick_models(resp.json().get("data", []), self.max_paid_price_per_mtok)
            cached = {"ts": time.time(), "free": free, "paid": paid}
            self.state.put("openrouter_models", cached)
        return (self._free or cached["free"]), (self._paid or (cached["paid"] if self._auto_paid else ""))

    def budget_left(self) -> float:
        return self.monthly_budget_usd - self.state.llm_spend_since(month_start())

    # ---- calls -----------------------------------------------------------------------------
    def _call(self, model: str, system: str, user: str, max_tokens: int, paid: bool = False) -> tuple[str, float]:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "usage": {"include": True},
        }
        resp = self._http.post("/chat/completions", json=body)
        if resp.status_code != 200:
            raise LLMError(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise LLMError(f"{model}: {data['error']}")
        choices = data.get("choices") or []
        text = (choices[0].get("message") or {}).get("content") if choices else None
        if not text:
            raise LLMError(f"{model}: empty completion")
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        if paid and cost == 0.0:  # no cost reported: assume the worst allowed price so the cap still holds
            cost = float(usage.get("total_tokens") or max_tokens * 2) * self.max_paid_price_per_mtok / 1e6
        return text, cost

    def complete(self, system: str, user: str, *, purpose: str, max_tokens: int = 6000,
                 allow_paid: bool = True, prefer_paid: bool = False) -> Completion:
        """Try free models, then the paid one. With prefer_paid (used for retries after a free model
        already failed at the task), the paid model goes first while budget remains."""
        free, paid = self.models()
        errors: list[str] = []
        use_paid = allow_paid and bool(paid)
        if use_paid and self.budget_left() <= 0.05:
            errors.append(f"paid model skipped: monthly LLM budget ${self.monthly_budget_usd:.2f} used up")
            use_paid = False
        order = [(m, False) for m in free]
        if use_paid:
            order = [(paid, True)] + order if prefer_paid else order + [(paid, True)]
        for model, is_paid in order:
            if not is_paid:
                wait = FREE_MIN_INTERVAL_S - (time.monotonic() - self._last_free_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_free_call = time.monotonic()
            try:
                text, cost = self._call(model, system, user, max_tokens, paid=is_paid)
                self.state.record_llm_call(model, purpose, cost, True)
                return Completion(text, model, cost)
            except (LLMError, httpx.HTTPError) as exc:
                self.state.record_llm_call(model, purpose, 0.0, False)
                errors.append(str(exc)[:160])
        raise LLMError("all models failed: " + " || ".join(errors))
