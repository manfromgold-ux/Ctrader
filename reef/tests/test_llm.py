import json

import httpx
import pytest

from reef.llm import LLMError, OpenRouter, extract_block, extract_json, pick_models
from reef.state import State

CATALOGUE = [
    {"id": "qwen/qwen3-coder:free", "context_length": 262000, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "context_length": 131000,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "tiny/model:free", "context_length": 8000, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "qwen/qwen3-coder", "context_length": 262000, "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
    {"id": "anthropic/expensive", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
]


def test_pick_models():
    free, paid = pick_models(CATALOGUE, max_paid_price_per_mtok=3.0)
    assert free[0] == "qwen/qwen3-coder:free"
    assert "tiny/model:free" not in free  # context too small
    assert paid == "qwen/qwen3-coder"


def test_extract_blocks():
    text = "intro\n```json\n{\"a\": 1,}\n```\n```python\nprint(1)\n```\nthen\n```python\nprint(2)\n```"
    assert extract_json(text) == {"a": 1}
    assert extract_block(text, "python") == "print(2)"
    assert extract_json('Sure: [{"domain": "x.pl"}] hope it helps') == [{"domain": "x.pl"}]
    assert extract_json("no json here") is None


def _client(handler):
    return httpx.Client(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(handler))


def test_falls_back_from_free_to_paid_and_tracks_cost(tmp_path):
    state = State(tmp_path / "db")
    seen = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOGUE})
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"].endswith(":free"):
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.012}})

    llm = OpenRouter("k", state, client=_client(handler), monthly_budget_usd=1.0)
    out = llm.complete("s", "u", purpose="test")
    assert out.text == "ok" and out.model == "qwen/qwen3-coder"
    assert seen[-1] == "qwen/qwen3-coder" and seen[0].endswith(":free")
    assert state.llm_spend_since(0) == pytest.approx(0.012)

    seen.clear()
    llm.complete("s", "u", purpose="retry", prefer_paid=True)
    assert seen == ["qwen/qwen3-coder"]  # paid first on retries


def test_budget_cap_blocks_paid(tmp_path):
    state = State(tmp_path / "db")
    state.record_llm_call("qwen/qwen3-coder", "earlier", 5.0, True)

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOGUE})
        if json.loads(request.content)["model"].endswith(":free"):
            return httpx.Response(503, text="down")
        raise AssertionError("paid model must not be called once the budget is spent")

    llm = OpenRouter("k", state, client=_client(handler), monthly_budget_usd=5.0)
    with pytest.raises(LLMError, match="budget"):
        llm.complete("s", "u", purpose="test")
