"""Test doubles: a scripted LLM, an Apify gateway that runs Actors locally with the real Apify SDK, and a
small tender-notice website whose layout can be switched to simulate a redesign."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from reef import actor_pkg
from reef.apify_gw import ActorStats, RunResult, StoreHit
from reef.llm import Completion, LLMError

# ---------------------------------------------------------------------------------------------------------
# A fake website with two layouts (v1 and a "redesigned" v2), plus a broken mode (v3).
# ---------------------------------------------------------------------------------------------------------
TENDERS = [
    {"id": i, "title": f"Road resurfacing lot {i}", "buyer": f"Municipality {i % 4}", "value": 100_000 + i * 2500,
     "deadline": f"2026-11-{10 + i:02d}"}
    for i in range(1, 13)
]
PER_PAGE = 6


def render_v1(page: int, q: str) -> str:
    rows = TENDERS[(page - 1) * PER_PAGE: page * PER_PAGE]
    cards = "".join(
        f'<article class="tender"><h2><a href="/tender/{t["id"]}">{t["title"]}</a></h2>'
        f'<span class="buyer">{t["buyer"]}</span><span class="value">EUR {t["value"]:,}</span>'
        f'<time datetime="{t["deadline"]}">{t["deadline"]}</time><p>{"Lorem ipsum dolor sit amet. " * 12}</p></article>'
        for t in rows
    )
    nxt = f'<a rel="next" href="/tenders?q={q}&page={page + 1}">Next</a>' if page * PER_PAGE < len(TENDERS) else ""
    return f"<html><head><title>Tenders</title></head><body><h1>Public tenders: {q}</h1>{cards}{nxt}</body></html>"


def render_v2(page: int, q: str) -> str:
    rows = TENDERS[(page - 1) * PER_PAGE: page * PER_PAGE]
    cards = "".join(
        f'<div class="notice-card"><h3 class="notice-title"><a href="/notice/{t["id"]}">{t["title"]}</a></h3>'
        f'<div data-field="authority">{t["buyer"]}</div><div data-field="budget">{t["value"]}</div>'
        f'<div data-field="deadline">{t["deadline"]}</div><p>{"Lorem ipsum dolor sit amet. " * 12}</p></div>'
        for t in rows
    )
    nxt = f'<a class="pager-next" href="/notices?query={q}&p={page + 1}">More</a>' if page * PER_PAGE < len(TENDERS) else ""
    return f"<html><body><main>{cards}{nxt}</main></body></html>"


class FakeSite:
    def __init__(self):
        self.layout = "v1"
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                qs = parse_qs(parts.query)
                if parts.path == "/robots.txt":
                    return self._send(200, "User-agent: *\nDisallow: /private/\n", "text/plain")
                if parts.path == "/" and site.layout != "v3":
                    links = ('<a href="/about">About us</a> <a href="/login">Log in</a> '
                             '<a href="/tenders?q=roads&page=1">Current public tenders</a>')
                    return self._send(200, f"<html><body><h1>City portal</h1>{links}"
                                           f"<p>{'Welcome to the portal. ' * 40}</p></body></html>")
                if site.layout == "v3":
                    return self._send(200, "<html><body><p>" + "Maintenance in progress. " * 100 + "</p></body></html>")
                if site.layout == "v1" and parts.path == "/tenders":
                    return self._send(200, render_v1(int(qs.get("page", ["1"])[0]), qs.get("q", [""])[0]))
                if site.layout == "v2" and parts.path in ("/notices", "/tenders"):
                    return self._send(200, render_v2(int(qs.get("p", ["1"])[0]), qs.get("query", qs.get("q", [""]))[0]))
                return self._send(404, "<html><body><h1>Not found</h1></body></html>")

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


# ---------------------------------------------------------------------------------------------------------
# Extractors the "model" writes.
# ---------------------------------------------------------------------------------------------------------
EXTRACTOR_V1 = '''
import re
from urllib.parse import urljoin, quote_plus
from parsel import Selector

BASE = "{base}"


def start_urls(params):
    q = params.get("query") or "roads"
    return [BASE + "/tenders?q=" + quote_plus(q) + "&page=1"]


def _money(text):
    digits = re.sub(r"[^0-9]", "", text or "")
    return float(digits) if digits else None


def parse(html, url):
    sel = Selector(text=html)
    items = []
    for card in sel.css("article.tender"):
        href = card.css("h2 a::attr(href)").get()
        items.append({{
            "url": urljoin(url, href) if href else None,
            "title": (card.css("h2 a::text").get() or "").strip() or None,
            "buyer": (card.css(".buyer::text").get() or "").strip() or None,
            "value_eur": _money(card.css(".value::text").get()),
            "deadline": card.css("time::attr(datetime)").get(),
        }})
    nxt = sel.css("a[rel=next]::attr(href)").get()
    return {{"items": items, "next": [urljoin(url, nxt)] if nxt else []}}
'''

EXTRACTOR_V2 = '''
from urllib.parse import urljoin, quote_plus
from parsel import Selector

BASE = "{base}"


def start_urls(params):
    q = params.get("query") or "roads"
    return [BASE + "/notices?query=" + quote_plus(q) + "&p=1"]


def parse(html, url):
    sel = Selector(text=html)
    items = []
    for card in sel.css("div.notice-card"):
        href = card.css("h3 a::attr(href)").get()
        budget = (card.css("[data-field=budget]::text").get() or "").strip()
        items.append({{
            "url": urljoin(url, href) if href else None,
            "title": (card.css("h3 a::text").get() or "").strip() or None,
            "buyer": (card.css("[data-field=authority]::text").get() or "").strip() or None,
            "value_eur": float(budget) if budget.isdigit() else None,
            "deadline": (card.css("[data-field=deadline]::text").get() or "").strip() or None,
        }})
    nxt = sel.css("a.pager-next::attr(href)").get()
    return {{"items": items, "next": [urljoin(url, nxt)] if nxt else []}}
'''

SPEC = {
    "title": "Municipal Tenders Scraper",
    "description": "Extract public procurement notices (title, contracting authority, value and deadline) "
                   "from the municipal tenders portal for bid monitoring.",
    "seo_title": "Municipal Tenders Scraper",
    "seo_description": "Scrape public tender notices with buyer, value and deadline.",
    "categories": ["LEAD_GENERATION", "BUSINESS", "NOT_A_CATEGORY"],
    "params": [{"name": "query", "title": "Keyword", "type": "string", "description": "Search keyword",
                "default": "roads", "required": False}],
    "fields": [
        {"name": "url", "type": "string", "description": "Notice URL", "required": True},
        {"name": "title", "type": "string", "description": "Notice title", "required": True},
        {"name": "buyer", "type": "string", "description": "Contracting authority", "required": True},
        {"name": "value_eur", "type": "number", "description": "Estimated value in EUR", "required": False},
        {"name": "deadline", "type": "string", "description": "Submission deadline (ISO date)", "required": True},
    ],
    "test_params": {"query": "roads"},
    "use_cases": ["Monitor new tenders for your industry", "Feed a CRM with bid opportunities"],
    "min_items": 3,
}


def build_reply(base: str) -> str:
    return f"Here you go.\n```json\n{json.dumps(SPEC)}\n```\n\n```python\n{EXTRACTOR_V1.format(base=base)}\n```"


class FakeLLM:
    """Scripted replies per purpose. Each purpose has a queue; the last reply repeats."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, purpose, max_tokens=6000, allow_paid=True, prefer_paid=False):
        self.calls.append((purpose, user))
        queue = self.replies.get(purpose)
        if not queue:
            raise LLMError(f"no scripted reply for {purpose}")
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        return Completion(text=text, model="fake/model:free", cost_usd=0.0)

    def models(self):
        return ["fake/model:free"], ""


# ---------------------------------------------------------------------------------------------------------
# Apify gateway that keeps Actors in memory and executes runs locally with the real Apify SDK.
# ---------------------------------------------------------------------------------------------------------
class FakeApify:
    def __init__(self, store_hits: list[StoreHit] | None = None):
        self.store_hits = store_hits or []
        self.sources: dict[str, list[dict]] = {}
        self.public: dict[str, bool] = {}
        self.priced: dict[str, bool] = {}
        self.deleted: list[str] = []
        self.retired: list[str] = []
        self.stats_by_id: dict[str, ActorStats] = {}
        self.runs: list[dict] = []
        self.pricing_ok = True
        self._n = 0

    def username(self):
        return "tester"

    def store_search(self, query, limit=10):
        return self.store_hits

    def deploy(self, spec, files, actor_id=""):
        if not actor_id:
            self._n += 1
            actor_id = f"act{self._n}"
        self.sources[actor_id] = files
        return actor_id

    def build(self, actor_id):
        with tempfile.TemporaryDirectory() as tmp:
            actor_pkg.write_tree(self.sources[actor_id], Path(tmp))
            proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "src"], cwd=tmp, capture_output=True,
                                  text=True)
            return proc.returncode == 0, proc.stdout + proc.stderr

    def run(self, actor_id, run_input, max_items, timeout_s=300):
        run_input = {**run_input, "proxyConfiguration": {"useApifyProxy": False}}
        self.runs.append({"actor_id": actor_id, "input": run_input})
        with tempfile.TemporaryDirectory() as tmp:
            root = actor_pkg.write_tree(self.sources[actor_id], Path(tmp) / "actor")
            storage = Path(tmp) / "storage"
            kv = storage / "key_value_stores" / "default"
            kv.mkdir(parents=True)
            (kv / "INPUT.json").write_text(json.dumps(run_input))
            env = {**os.environ, "APIFY_LOCAL_STORAGE_DIR": str(storage), "CRAWLEE_PURGE_ON_START": "0"}
            proc = subprocess.run([sys.executable, "-m", "src"], cwd=root, env=env, capture_output=True, text=True,
                                  timeout=timeout_s)
            items = []
            for f in sorted((storage / "datasets" / "default").glob("*.json")):
                if not f.name.startswith("__"):
                    items.append(json.loads(f.read_text()))
            pages = []
            for i in range(1, 4):
                html_files = [p for p in kv.glob(f"DEBUG_HTML_{i}*") if "metadata" not in p.name]
                url_files = [p for p in kv.glob(f"DEBUG_URL_{i}*") if "metadata" not in p.name]
                if html_files:
                    url = json.loads(url_files[0].read_text())["url"] if url_files else ""
                    pages.append((url, html_files[0].read_text()))
        status = "SUCCEEDED" if proc.returncode == 0 else "FAILED"
        return RunResult(status=status, items=items[:max_items], debug_pages=pages,
                         status_message=(proc.stderr or proc.stdout)[-300:] if status == "FAILED" else "")

    def set_pricing(self, actor_id, price_per_1000):
        self.priced[actor_id] = self.pricing_ok
        return (True, "") if self.pricing_ok else (False, "HTTP 400: pricing not allowed")

    def publish(self, actor_id, spec):
        self.public[actor_id] = True
        return True, ""

    def retire(self, actor_id):
        self.public[actor_id] = False
        self.retired.append(actor_id)

    def delete(self, actor_id):
        self.deleted.append(actor_id)
        self.sources.pop(actor_id, None)

    def stats(self, actor_id):
        s = self.stats_by_id.get(actor_id, ActorStats())
        s.has_pricing = self.priced.get(actor_id, False)
        return s

    def monthly_usage_usd(self):
        return 1.25
