import pytest

from reef import sandbox

GOOD = '''
from parsel import Selector

def start_urls(params):
    return ["https://example.com/list?q=" + params.get("q", "x")]

def parse(html, url):
    sel = Selector(text=html)
    return {"items": [{"title": t} for t in sel.css("h2::text").getall()], "next": []}
'''


@pytest.mark.parametrize("bad, reason", [
    ("import os\ndef start_urls(p): return []\ndef parse(h, u): return {}", "import of 'os'"),
    ("import socket\ndef start_urls(p): return []\ndef parse(h, u): return {}", "socket"),
    ("from subprocess import run\ndef start_urls(p): return []\ndef parse(h, u): return {}", "subprocess"),
    ("def start_urls(p): return open('/etc/passwd').read()\ndef parse(h, u): return {}", "'open'"),
    ("def start_urls(p): return ().__class__.__bases__\ndef parse(h, u): return {}", "dunder"),
    ("def start_urls(p): return eval('1')\ndef parse(h, u): return {}", "'eval'"),
    ("def start_urls(p): return getattr(p, 'x')\ndef parse(h, u): return {}", "'getattr'"),
    ("def start_urls(p): return []", "missing required"),
    ("def start_urls(p) return []", "SyntaxError"),
    ("from . import x\ndef start_urls(p): return []\ndef parse(h, u): return {}", "not allowed"),
])
def test_rejects_unsafe_code(bad, reason):
    with pytest.raises(sandbox.UnsafeCode, match=reason):
        sandbox.check_code(bad)


def test_runs_good_code():
    res = sandbox.run(GOOD, "start_urls", {"params": {"q": "bikes"}})
    assert res.ok and res.data == ["https://example.com/list?q=bikes"]
    res = sandbox.run(GOOD, "parse", {"pages": [{"url": "https://example.com", "html": "<h2>A</h2><h2>B</h2>"}]})
    assert res.ok
    assert res.data[0]["items"] == [{"title": "A"}, {"title": "B"}]


def test_child_has_no_secrets(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "super-secret")
    code = GOOD.replace('return ["https://example.com/list?q=" + params.get("q", "x")]',
                        'import json\n    return [json.dumps(sorted(__import__("os").environ))]')
    # __import__ is blocked statically, so the leak attempt never runs
    res = sandbox.run(code, "start_urls", {"params": {}})
    assert not res.ok and "rejected by safety check" in res.error


def test_reports_runtime_errors_and_bad_shapes():
    crash = GOOD.replace('return {"items"', 'raise ValueError("boom")\n    return {"items"')
    res = sandbox.run(crash, "parse", {"pages": [{"url": "u", "html": "<p/>"}]})
    assert not res.ok and "ValueError: boom" in res.error
    wrong = GOOD.replace('return {"items": [{"title": t} for t in sel.css("h2::text").getall()], "next": []}',
                         'return ["not a dict"]')
    res = sandbox.run(wrong, "parse", {"pages": [{"url": "u", "html": "<p/>"}]})
    assert not res.ok and "must return a dict" in res.error


def test_timeout():
    loop = GOOD.replace('sel = Selector(text=html)', 'while True:\n        pass')
    res = sandbox.run(loop, "parse", {"pages": [{"url": "u", "html": "<p/>"}]}, timeout=3)
    assert not res.ok and ("timed out" in res.error or "no output" in res.error)
