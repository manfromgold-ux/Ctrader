"""Scout: find public data sources nobody serves well on Apify Store, and check that they can be fetched."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urldefrag, urlsplit

from lxml import etree
from lxml import html as lxml_html

from ..context import Context
from ..fetch import FetchResult, host_of, is_blocked_domain, same_site, visible_text_length
from ..llm import LLMError, extract_json, extract_objects
from ..prompts import SCOUT_SYSTEM, scout_user
from . import Retry

log = logging.getLogger("reef.scout")
STRONG_INCUMBENT_USERS30 = 200
MIN_VISIBLE_TEXT = 1500
# Link words that usually lead to listing pages (several languages), and links never worth following.
LISTING_WORDS = (
    "tender", "procure", "notice", "auction", "job", "vacanc", "career", "offer", "listing", "search", "catalog",
    "product", "categor", "director", "compan", "event", "result", "list", "zakup", "przetarg", "ausschreib",
    "licita", "concurso", "annonce", "angebot", "oferta", "ogloszen", "immobil", "property", "vergabe", "zakazk",
)
SKIP_LINK_WORDS = (
    "login", "signin", "sign-in", "register", "account", "contact", "privacy", "cookie", "terms", "about", "help",
    "faq", "cart", "basket", "mailto:", "tel:", "javascript:", ".pdf", ".jpg", ".png", ".zip", ".doc", ".xls",
)


def ideas_from(text: str) -> list[dict]:
    """Site ideas from a model reply: the whole JSON list if it parses, else every complete entry."""
    raw = extract_json(text)
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    ideas = [i for i in raw or [] if isinstance(i, dict)] if isinstance(raw, list) else []
    if not ideas:
        ideas = extract_objects(text)
    return [i for i in ideas if i.get("start_url") or i.get("domain")]


def competition(ctx: Context, domain: str, terms: list[str]) -> tuple[int, int]:
    """Return (number of Store Actors that target this domain, best 30-day user count among them)."""
    stem = re.sub(r"\.[a-z.]+$", "", domain)  # "example.co.uk" -> "example"
    seen: dict[str, int] = {}
    for query in [domain, *terms[:1]]:
        try:
            hits = ctx.apify.store_search(query, limit=20)
        except Exception as exc:
            log.warning("store search failed for %r: %s", query, exc)
            continue
        for h in hits:
            text = f"{h.title} {h.name} {h.description}".lower()
            if domain in text or (len(stem) >= 4 and stem in text):
                seen[f"{h.username}/{h.name}"] = h.users30
    return len(seen), max(seen.values(), default=0)


def discover_listing(ctx: Context, domain: str, idea: dict) -> FetchResult | None:
    """The model's guessed URL failed: open the homepage and follow its most listing-like links."""
    home = None
    parts = urlsplit(str(idea.get("start_url") or ""))
    roots = [f"{parts.scheme}://{parts.netloc}/"] if parts.scheme in ("http", "https") and parts.netloc else []
    for url in dict.fromkeys(roots + [f"https://{domain}/", f"https://www.{domain}/"]):
        page = ctx.fetcher.get(url)
        if page.ok:
            home = page
            break
    if home is None:
        return None
    hints = {w for w in re.findall(r"[a-z\u00c0-\u024f]{4,}",
                                   f"{idea.get('data', '')} {' '.join(map(str, idea.get('search_terms') or []))} "
                                   f"{idea.get('start_url', '')}".lower())} - {"https", "http", "www"}
    try:
        doc = lxml_html.fromstring(home.html, base_url=home.final_url or f"https://{domain}/")
        doc.make_links_absolute()
    except (etree.ParserError, ValueError):
        return None
    scored: dict[str, int] = {}
    for a in doc.iter("a"):
        href = urldefrag(a.get("href") or "")[0]
        text = a.text_content().lower()
        low = href.lower()
        if not href.startswith(("http://", "https://")) or not same_site(href, domain):
            continue
        if any(w in low or w in text for w in SKIP_LINK_WORDS):
            continue
        score = sum(2 for w in hints if w in low or w in text) + sum(1 for w in LISTING_WORDS if w in low or w in text)
        if score:
            scored[href] = max(score, scored.get(href, 0))
    for href, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:4]:
        page = ctx.fetcher.get(href)
        if page.ok and visible_text_length(page.html) >= MIN_VISIBLE_TEXT:
            return page
    return home if visible_text_length(home.html) >= MIN_VISIBLE_TEXT else None


def evaluate(ctx: Context, idea: dict, source: str = "scout") -> str:
    """Check one idea and store it as a candidate. Returns 'new', 'rejected' or 'skipped'."""
    start_url = str(idea.get("start_url") or "").strip()
    domain = str(idea.get("domain") or host_of(start_url)).lower().removeprefix("www.").strip("/")
    if not domain or not start_url.startswith(("http://", "https://")) or not same_site(start_url, domain):
        return "skipped"
    if domain in ctx.state.known_domains():
        return "skipped"
    summary = str(idea.get("data") or "")[:500]
    buyers = str(idea.get("buyers") or "")[:500]

    def reject(reason: str) -> str:
        ctx.state.add_candidate(domain, start_url, summary, buyers, 0, source=source, status="rejected", reason=reason)
        log.info("rejected %s: %s", domain, reason)
        return "rejected"

    if is_blocked_domain(domain, ctx.cfg.blocked_domains):
        return reject("domain is on the block list")
    rivals, rival_users = competition(ctx, domain, [str(t) for t in idea.get("search_terms") or []])
    if rival_users >= STRONG_INCUMBENT_USERS30:
        return reject(f"strong incumbent on Apify Store ({rival_users} users/30d)")

    page = ctx.fetcher.get(start_url)
    if not page.ok and not page.blocked:  # usually a guessed path that does not exist (404) or is off-limits
        found = discover_listing(ctx, domain, idea)
        if found is not None:
            log.info("%s: suggested page failed (%s), using %s instead", domain, page.describe(), found.final_url)
            page = found
    if not page.ok:
        return reject(f"start page not usable: {page.describe()}")
    text_len = visible_text_length(page.html)
    if text_len < MIN_VISIBLE_TEXT:
        return reject(f"page has little server-rendered text ({text_len} chars) - probably needs a browser")

    score = 1.0 if rivals == 0 else 0.6 if rival_users < 20 else 0.3
    score += min(text_len, 50_000) / 50_000 * 0.2
    if source == "clone":
        score += 0.3
    ctx.state.add_candidate(domain, page.final_url or start_url, summary, buyers, round(score, 3), source=source)
    ctx.state.log("candidate", f"{domain} (score {score:.2f}, {rivals} rival Actors): {summary[:120]}")
    return "new"


def _save_reply(ctx: Context, purpose: str, text: str) -> None:
    """Keep unreadable model replies in data/debug/ so they can be inspected."""
    folder = ctx.cfg.data_dir / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{purpose}-{time.strftime('%Y%m%d-%H%M%S')}.txt").write_text(text, encoding="utf-8")


def run(ctx: Context, n: int = 15) -> int | Retry:
    exclude = sorted(ctx.state.known_domains())
    log.info("asking the AI for %d site ideas...", n)
    try:
        reply = ctx.llm.complete(SCOUT_SYSTEM, scout_user(n, exclude, ctx.cfg.blocked_domains),
                                 purpose="scout", max_tokens=8000, allow_paid=False)
    except LLMError as exc:
        ctx.state.log("error", f"scout: {exc}")
        return Retry(1, "models unavailable")
    ideas = ideas_from(reply.text)
    if not ideas:
        _save_reply(ctx, "scout", reply.text)
        ctx.state.log("error", f"scout: no site list in the reply from {reply.model}: {reply.text[:200]!r}")
        return Retry(1, "unusable model reply")
    log.info("got %d ideas, checking each site (Apify Store, robots.txt, the page itself)...", len(ideas))
    added = 0
    for idea in ideas:
        if evaluate(ctx, idea) == "new":
            added += 1
            log.info("accepted %s", idea.get("domain") or idea.get("start_url"))
    ctx.state.log("scout", f"{added} new candidates out of {len(ideas)} ideas from {reply.model}")
    return added if added else Retry(2, "every idea was rejected")
