"""Polite HTTP fetching (robots.txt, per-host delay, size cap) and HTML trimming for prompts."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import etree
from lxml import html as lxml_html

# Same browser-like UA as the Actor engine, so canary checks see the page the Actor sees.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)
BLOCK_MARKERS = (
    "cf-chl-", "challenge-platform", "captcha", "are you a robot", "access denied", "just a moment...",
    "request unsuccessful. incapsula", "px-captcha", "datadome",
)
KEEP_ATTRS = {
    "id", "class", "href", "itemprop", "itemscope", "itemtype", "datetime", "content", "name", "property",
    "aria-label", "title", "rel", "type", "role", "data-id", "data-testid", "data-price", "value", "alt",
}
DROP_TAGS = ("style", "svg", "noscript", "iframe", "canvas", "template", "link", "meta[@charset]")


@dataclass
class FetchResult:
    url: str
    status: int
    html: str = ""
    final_url: str = ""
    blocked: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.blocked and not self.error and bool(self.html)

    def describe(self) -> str:
        if self.error:
            return f"fetch error: {self.error}"
        if self.blocked:
            return f"blocked by anti-bot protection (HTTP {self.status})"
        return f"HTTP {self.status}"


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, domain: str) -> bool:
    host = host_of(url)
    domain = domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def is_blocked_domain(domain: str, blocked: list[str]) -> bool:
    domain = domain.lower().removeprefix("www.")
    return any(domain == b or domain.endswith("." + b) for b in blocked)


def looks_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 407, 429, 503):
        return True
    head = text[:20_000].lower()
    return any(marker in head for marker in BLOCK_MARKERS) and len(head) < 15_000


class Fetcher:
    def __init__(self, min_delay: float = 2.0, timeout: float = 25.0, max_bytes: int = 4_000_000,
                 respect_robots: bool = True, client: httpx.Client | None = None):
        self.min_delay = min_delay
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8,*;q=0.5"},
        )
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def close(self) -> None:
        self._client.close()

    def _wait_turn(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            delay = self.min_delay - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last_hit[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        """robots.txt check following RFC 9309: 4xx => allow all, 5xx/unreachable => disallow all."""
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                self._wait_turn(parts.netloc)
                resp = self._client.get(origin + "/robots.txt")
                if resp.status_code >= 500:
                    parser = None
                elif resp.status_code >= 400:
                    parser.parse([])
                else:
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is not None and parser.can_fetch("*", url)

    def get(self, url: str) -> FetchResult:
        if not url.startswith(("http://", "https://")):
            return FetchResult(url=url, status=0, error="not an http(s) URL")
        if not self.allowed(url):
            return FetchResult(url=url, status=0, error="disallowed by robots.txt")
        self._wait_turn(urlsplit(url).netloc)
        try:
            with self._client.stream("GET", url) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        return FetchResult(url=url, status=resp.status_code, error="page larger than size cap")
                    chunks.append(chunk)
                body = b"".join(chunks)
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype and not body.lstrip()[:15].lower().startswith(b"<"):
                    return FetchResult(url=url, status=resp.status_code, error=f"not HTML ({ctype})")
                text = body.decode(resp.encoding or "utf-8", errors="replace")
                return FetchResult(url=url, status=resp.status_code, html=text, final_url=str(resp.url),
                                   blocked=looks_blocked(resp.status_code, text))
        except httpx.HTTPError as exc:
            return FetchResult(url=url, status=0, error=f"{type(exc).__name__}: {exc}")


def visible_text_length(page_html: str) -> int:
    try:
        doc = lxml_html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return 0
    for bad in doc.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    return len(re.sub(r"\s+", " ", doc.text_content()).strip())


def trim_html(page_html: str, max_chars: int) -> str:
    """Shrink HTML for an LLM prompt while keeping everything selectors depend on (tags, ids, classes,
    itemprops, hrefs) and JSON-LD blocks. Long text runs are shortened."""
    try:
        doc = lxml_html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return page_html[:max_chars]
    for node in doc.xpath("//comment()"):
        node.getparent().remove(node)
    for node in doc.xpath("//script"):
        if (node.get("type") or "").lower() != "application/ld+json":
            node.drop_tree()
    for tag in DROP_TAGS:
        for node in doc.xpath(f"//{tag}"):
            node.drop_tree()
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr not in KEEP_ATTRS and not attr.startswith("data-"):
                del el.attrib[attr]
            elif len(el.attrib.get(attr, "")) > 200:
                el.attrib[attr] = el.attrib[attr][:200]
        if el.text and len(el.text) > 300 and el.tag != "script":
            el.text = el.text[:300] + "…"
    out = lxml_html.tostring(doc, encoding="unicode")
    out = re.sub(r"\s{2,}", " ", out)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n<!-- truncated -->"
    return out
