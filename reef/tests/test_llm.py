import json

import httpx
import pytest

from reef.llm import (
    LLMError,
    OpenRouter,
    extract_block,
    extract_code,
    extract_json,
    extract_objects,
    extract_spec,
    pick_models,
)
from reef.state import State


def _client(handler):
    return httpx.Client(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(handler))


TEXT = {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]}
CATALOGUE = [
    {"id": "qwen/qwen3-coder:free", "context_length": 262000, "architecture": TEXT,
     "pricing": {"prompt": "0", "completion": "0", "request": "0"}},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "context_length": 131000, "architecture": TEXT,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "tiny/model:free", "context_length": 8000, "architecture": TEXT, "pricing": {"prompt": "0", "completion": "0"}},
    # The real-world trap: a music model with $0 token prices but a per-clip fee and audio output.
    {"id": "google/lyria-3-clip-preview", "context_length": 1000000,
     "architecture": {"modality": "text->audio", "input_modalities": ["text"], "output_modalities": ["audio"]},
     "pricing": {"prompt": "0", "completion": "0", "request": "0.04"}},
    {"id": "some/image-gen:free", "context_length": 100000,
     "architecture": {"modality": "text->image", "output_modalities": ["image"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "promo/zero-price-chat", "context_length": 100000, "architecture": TEXT,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "qwen/qwen3-coder", "context_length": 262000, "architecture": TEXT,
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},
    {"id": "qwen/qwen3-coder-with-fee", "context_length": 262000, "architecture": TEXT,
     "pricing": {"prompt": "0.0000003", "completion": "0.0000012", "request": "0.01"}},
    {"id": "anthropic/expensive", "context_length": 200000, "architecture": TEXT,
     "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
]


def test_pick_models_only_true_free_text_models():
    free, paid = pick_models(CATALOGUE, max_paid_price_per_mtok=3.0)
    assert free == ["qwen/qwen3-coder:free", "meta-llama/llama-3.3-70b-instruct:free"]
    assert paid == ["qwen/qwen3-coder"]


def _chat(text, cost=0.0):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {"cost": cost}})


def test_probe_drops_models_that_cannot_chat(tmp_path, monkeypatch):
    monkeypatch.setattr("reef.llm.FREE_MIN_INTERVAL_S", 0)
    state = State(tmp_path / "db")

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOGUE})
        model = json.loads(request.content)["model"]
        if model == "qwen/qwen3-coder:free":
            return httpx.Response(429, json={"error": "rate limited upstream"})  # busy, kept as backup
        if model.startswith("meta-llama"):
            return _chat("Sure! Here is a poem instead.")  # cannot follow instructions: dropped
        return _chat('{"ok": true}', 0.0001)

    llm = OpenRouter("k", state, client=_client(handler))
    free, paid = llm.models()
    assert free == ["qwen/qwen3-coder:free"] and paid == "qwen/qwen3-coder"
    assert state.get("openrouter_models_v3")["free"] == free  # cached under the new key


def test_extract_blocks():
    text = "intro\n```json\n{\"a\": 1,}\n```\n```python\nprint(1)\n```\nthen\n```python\nprint(2)\n```"
    assert extract_json(text) == {"a": 1}
    assert extract_block(text, "python") == "print(2)"
    assert extract_json('Sure: [{"domain": "x.pl"}] hope it helps') == [{"domain": "x.pl"}]
    assert extract_json("no json here") is None


def test_falls_back_from_free_to_paid_and_tracks_cost(tmp_path, monkeypatch):
    monkeypatch.setattr("reef.llm.FREE_MIN_INTERVAL_S", 0)
    monkeypatch.setattr("reef.llm.RATE_LIMIT_RETRY_WAIT_S", 0)
    state = State(tmp_path / "db")
    seen = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": CATALOGUE})
        body = json.loads(request.content)
        if body["messages"][1]["content"] == "ping":
            return _chat('{"ok": true}', 0.0 if body["model"].endswith(":free") else 0.0001)
        seen.append(body["model"])
        if body["model"].endswith(":free"):
            return httpx.Response(429, json={"error": "rate limited"})
        return _chat("ok", 0.012)

    llm = OpenRouter("k", state, client=_client(handler), monthly_budget_usd=1.0)
    out = llm.complete("s", "u", purpose="test")
    assert out.text == "ok" and out.model == "qwen/qwen3-coder"
    assert seen == ["qwen/qwen3-coder:free", "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-coder"]
    assert state.llm_spend_since(0) == pytest.approx(0.012 + 0.0001)  # task + one paid probe

    seen.clear()
    llm.complete("s", "u", purpose="retry", prefer_paid=True)
    assert seen == ["qwen/qwen3-coder"]  # paid first on retries


def test_free_only_gets_second_pass_after_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("reef.llm.FREE_MIN_INTERVAL_S", 0)
    monkeypatch.setattr("reef.llm.RATE_LIMIT_RETRY_WAIT_S", 0)
    calls = []

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _chat("hello")

    llm = OpenRouter("k", State(tmp_path / "db"), free_models=["a/b:free"], paid_model="", client=_client(handler))
    assert llm.complete("s", "u", purpose="t", allow_paid=False).text == "hello"
    assert calls == ["a/b:free", "a/b:free"]


def test_budget_cap_blocks_paid(tmp_path, monkeypatch):
    monkeypatch.setattr("reef.llm.FREE_MIN_INTERVAL_S", 0)
    monkeypatch.setattr("reef.llm.RATE_LIMIT_RETRY_WAIT_S", 0)
    state = State(tmp_path / "db")
    state.record_llm_call("qwen/qwen3-coder", "earlier", 5.0, True)

    def handler(request):
        body = json.loads(request.content)
        if body["model"].endswith(":free"):
            return httpx.Response(503, text="down")
        raise AssertionError("paid model must not be called once the budget is spent")

    llm = OpenRouter("k", state, free_models=["x/y:free"], paid_model="qwen/qwen3-coder", client=_client(handler),
                     monthly_budget_usd=5.0)
    with pytest.raises(LLMError, match="budget"):
        llm.complete("s", "u", purpose="test")


def test_salvages_truncated_or_partly_broken_lists():
    from reef.jobs.scout import ideas_from

    cut_off = (
        '```json\n[\n  {\n    "domain": "tender.gov.ua",\n    "start_url": "https://tender.gov.ua/procurements",\n'
        '    "data": "tenders {with braces} and \\"quotes\\"", "search_terms": ["tenders", "ua"]\n  },\n'
        '  {"domain": "broken.pl", "start_url": "https://broken.pl" "missing": "comma"},\n'
        '  {"domain": "ok.cz", "start_url": "https://ok.cz/list", "data": "x",},\n'
        '  {"domain": "cut.de", "start_url": "https://cut.de/li'
    )
    assert extract_json(cut_off) is None
    assert [o["domain"] for o in extract_objects(cut_off)] == ["tender.gov.ua", "ok.cz"]
    assert [i["domain"] for i in ideas_from(cut_off)] == ["tender.gov.ua", "ok.cz"]
    assert ideas_from('{"sites": [{"domain": "a.pl", "start_url": "https://a.pl"}]}')[0]["domain"] == "a.pl"
    assert ideas_from("I only make music") == []


def test_overloaded_counts_as_busy_and_thinking_models_get_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("reef.llm.FREE_MIN_INTERVAL_S", 0)
    monkeypatch.setattr("reef.llm.RATE_LIMIT_RETRY_WAIT_S", 0)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        if len(calls) == 1:
            return httpx.Response(200, json={"error": {"message": "Upstream error: Service temporarily overloaded",
                                                       "code": 503}})
        if len(calls) == 2:
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return _chat("hello")

    llm = OpenRouter("k", State(tmp_path / "db"), free_models=["a/b:free"], paid_model="", client=_client(handler))
    with pytest.raises(LLMError, match="tokens thinking"):
        llm.complete("s", "u", purpose="t", allow_paid=False)  # overloaded -> retried -> ran out of tokens
    assert calls == ["a/b:free", "a/b:free"]
    assert llm.complete("s", "u", purpose="t", allow_paid=False).text == "hello"


def test_tolerant_reply_parsing():
    code = "def start_urls(p):\n    return []\n\ndef parse(h, u):\n    return {}"
    spec = '{"title": "X Scraper", "fields": [{"name": "url"}]}'
    assert extract_code(f"```py\n{code}\n```") == code
    assert extract_code(f"```\n{spec}\n```\n```\n{code}\n```") == code  # untagged fences
    assert extract_code(f"```python\n{code}") == code  # cut off before the closing fence
    assert extract_code("```python\nprint(1)\n```") == "print(1)"
    assert extract_code("no code") is None
    assert extract_spec(f"Here:\n{spec}\nand code") == {"title": "X Scraper", "fields": [{"name": "url"}]}
    assert extract_spec(f"```JSON\n{spec}\n```")["title"] == "X Scraper"


def test_trim_html_survives_comments_outside_root():
    from reef.fetch import trim_html

    out = trim_html("<!-- top --><!DOCTYPE html><html><body><!-- in --><p class='a'>hi</p></body></html>", 1000)
    assert "hi" in out and "in -->" not in out
