"""OpenRouter client: free models first, a paid model as fallback, and a hard monthly spending cap."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .state import DAY, State, month_start

log = logging.getLogger("reef.llm")
API = "https://openrouter.ai/api/v1"
# Preference order when picking models automatically from OpenRouter's live catalogue.
FREE_PREFERENCES = [r"coder", r"qwen3", r"deepseek", r"gpt-oss", r"kimi", r"glm", r"llama-3\.3-70b", r"gemma"]
PAID_PREFERENCES = [r"qwen3-coder", r"deepseek-(chat|v3)", r"kimi-k2", r"gpt-oss-120b", r"glm-4", r"gemini-.*flash"]
# Price components that must be zero even for a paid model: Reef only pays per token, never per call.
PER_CALL_FEES = ("request", "image", "audio", "web_search")
FREE_MIN_INTERVAL_S = 3.2  # OpenRouter allows 20 requests/minute on free models
RATE_LIMIT_RETRY_WAIT_S = 20.0
MODELS_CACHE_KEY = "openrouter_models_v3"  # bump to discard cached picks made by older selection logic
PROBE_SYSTEM = 'Reply with exactly this JSON and nothing else: {"ok": true}'
PROBE_TIMEOUT_S = 45.0  # a model that cannot answer "ping" in 45s is no use to us today


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


def _prices(pricing: dict) -> dict[str, float] | None:
    out = {}
    for key, value in (pricing or {}).items():
        try:
            out[key] = float(value or 0)
        except (TypeError, ValueError):
            return None
    return out


def is_text_chat_model(m: dict) -> bool:
    """True only for text-in/text-out chat models (excludes music, image, video and embedding models)."""
    arch = m.get("architecture") or {}
    outputs = arch.get("output_modalities")
    if outputs is not None:
        return list(outputs) == ["text"] and "text" in (arch.get("input_modalities") or ["text"])
    modality = str(arch.get("modality") or "")
    return modality.startswith("text") and modality.endswith("->text")


def pick_models(catalogue: list[dict], max_paid_price_per_mtok: float,
                n_free: int = 10) -> tuple[list[str], list[str]]:
    """Choose free-model candidates and up to 3 paid candidates from OpenRouter's /models payload.

    Free means an official ':free' variant whose every price component is zero. Both lists contain only
    text chat models; anything with per-request, image or audio fees is skipped.
    """
    free, paid = [], []
    for m in catalogue:
        mid = m.get("id", "")
        ctx = int(m.get("context_length") or 0)
        prices = _prices(m.get("pricing") or {})
        if ctx < 32_000 or prices is None or not is_text_chat_model(m):
            continue
        if any(prices.get(k, 0) > 0 for k in PER_CALL_FEES):
            continue
        if mid.endswith(":free"):
            if all(v == 0 for v in prices.values()):
                free.append((_match_order(mid, FREE_PREFERENCES), -ctx, mid))
            continue
        completion_per_m = prices.get("completion", 1) * 1e6
        prompt_per_m = prices.get("prompt", 1) * 1e6
        if (0 < completion_per_m <= max_paid_price_per_mtok and prompt_per_m <= max_paid_price_per_mtok
                and _match_order(mid, PAID_PREFERENCES) < len(PAID_PREFERENCES)):
            paid.append((_match_order(mid, PAID_PREFERENCES), completion_per_m, mid))
    free.sort()
    paid.sort()
    return [m for _, _, m in free[:n_free]], [m for _, _, m in paid[:3]]


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


def extract_objects(text: str) -> list[dict]:
    """Every complete top-level {...} object in the text that parses as JSON. Salvages lists that were
    cut off mid-way (token limit) or that contain one malformed entry."""
    objects, depth, start, in_str, escaped = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                chunk = re.sub(r",\s*([}\]])", r"\1", text[start:i + 1])
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    objects.append(obj)
    return objects


def _transient(exc: Exception) -> bool:
    """Rate limits and overloaded/unavailable upstreams: the model is fine, just busy right now."""
    text = str(exc).lower()
    return any(m in text for m in ("429", "rate", "503", "502", "overloaded", "temporarily", "timed out", "timeout"))


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
        """Free models to try (in order) and the paid fallback. Picked from the live catalogue; every
        candidate is test-called once a day so non-chat or broken models never get real work."""
        if self._free and (self._paid or not self._auto_paid):
            return self._free, self._paid
        cached = self.state.get(MODELS_CACHE_KEY)
        if not cached or time.time() - cached["ts"] > DAY:
            resp = self._http.get("/models")
            resp.raise_for_status()
            free, paid_candidates = pick_models(resp.json().get("data", []), self.max_paid_price_per_mtok)
            free = self._free or self._probe(free, keep=5)
            paid_ok = self._probe(paid_candidates, keep=1, paid=True, keep_busy=False) if self._auto_paid else []
            paid = paid_ok[0] if paid_ok else ""
            cached = {"ts": time.time(), "free": free, "paid": paid}
            self.state.put(MODELS_CACHE_KEY, cached)
        return (self._free or cached["free"]), (self._paid or (cached["paid"] if self._auto_paid else ""))

    def _probe(self, candidates: list[str], keep: int, paid: bool = False, keep_busy: bool = True) -> list[str]:
        """Keep models that answer a tiny JSON task. Rate-limited ones are kept as backups (busy is not
        broken); anything that errors otherwise or answers wrongly is dropped."""
        working, busy = [], []
        for i, model in enumerate(candidates, 1):
            if len(working) >= keep:
                break
            if not paid:
                self._wait_free_slot()
            log.info("testing model %d/%d: %s", i, len(candidates), model)
            try:
                text, cost = self._call(model, PROBE_SYSTEM, "ping", max_tokens=1000, paid=paid,
                                        timeout=PROBE_TIMEOUT_S)
                self.state.record_llm_call(model, "probe", cost, True)
                if extract_json(text) == {"ok": True}:
                    working.append(model)
                    log.info("  ok")
                else:
                    log.info("  dropped: answered %r", text[:60])
            except (LLMError, httpx.HTTPError) as exc:
                self.state.record_llm_call(model, "probe", 0.0, False)
                if _transient(exc):
                    busy.append(model)
                    log.info("  busy (kept as backup)")
                else:
                    log.info("  dropped: %s", str(exc)[:120])
        return (working + (busy if keep_busy else []))[:keep]

    def _wait_free_slot(self) -> None:
        wait = FREE_MIN_INTERVAL_S - (time.monotonic() - self._last_free_call)
        if wait > 0:
            time.sleep(wait)
        self._last_free_call = time.monotonic()

    def budget_left(self) -> float:
        return self.monthly_budget_usd - self.state.llm_spend_since(month_start())

    # ---- calls -----------------------------------------------------------------------------
    def _call(self, model: str, system: str, user: str, max_tokens: int, paid: bool = False,
              timeout: float | None = None) -> tuple[str, float]:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "usage": {"include": True},
        }
        resp = self._http.post("/chat/completions", json=body,
                               **({"timeout": httpx.Timeout(timeout, connect=20)} if timeout else {}))
        if resp.status_code != 200:
            raise LLMError(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise LLMError(f"{model}: {data['error']}")
        choices = data.get("choices") or []
        text = (choices[0].get("message") or {}).get("content") if choices else None
        if not text:
            if choices and choices[0].get("finish_reason") == "length":
                raise LLMError(f"{model}: used all {max_tokens} tokens thinking before answering")
            raise LLMError(f"{model}: empty completion")
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        if paid and cost == 0.0:  # no cost reported: assume the worst allowed price so the cap still holds
            cost = float(usage.get("total_tokens") or max_tokens * 2) * self.max_paid_price_per_mtok / 1e6
        return text, cost

    def complete(self, system: str, user: str, *, purpose: str, max_tokens: int = 6000,
                 allow_paid: bool = True, prefer_paid: bool = False) -> Completion:
        """Try free models, then the paid one. With prefer_paid (used for retries after a free model
        already failed at the task), the paid model goes first while budget remains. Free models are often
        briefly rate-limited upstream, so they get a second pass after a short pause."""
        free, paid = self.models()
        errors: list[str] = []
        use_paid = allow_paid and bool(paid)
        if use_paid and self.budget_left() <= 0.05:
            errors.append(f"paid model skipped: monthly LLM budget ${self.monthly_budget_usd:.2f} used up")
            use_paid = False
        free_pass = [(m, False) for m in free]
        order = list(free_pass)
        if use_paid:
            order = [(paid, True)] + order if prefer_paid else order + [(paid, True)]
        for attempt in range(2):
            rate_limited = False
            for model, is_paid in order:
                if not is_paid:
                    self._wait_free_slot()
                try:
                    text, cost = self._call(model, system, user, max_tokens, paid=is_paid)
                    self.state.record_llm_call(model, purpose, cost, True)
                    return Completion(text, model, cost)
                except (LLMError, httpx.HTTPError) as exc:
                    self.state.record_llm_call(model, purpose, 0.0, False)
                    errors.append(str(exc)[:160])
                    rate_limited |= not is_paid and _transient(exc)
            if attempt == 0 and rate_limited:
                time.sleep(RATE_LIMIT_RETRY_WAIT_S)
                order = free_pass  # second pass: free models only; the paid one already had its turn
            else:
                break
        raise LLMError("all models failed: " + " || ".join(errors))
