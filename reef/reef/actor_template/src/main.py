"""Generic crawl engine shared by every Reef Actor.

Site-specific logic lives in extractor.py (start_urls + parse). The engine handles input, proxies,
retries, pagination, de-duplication, the field contract and pay-per-result billing.
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag

import httpx
from apify import Actor

from . import extractor

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)
RESERVED = {"maxItems", "maxPages", "proxyConfiguration", "debugSaveHtml"}
META = json.loads((Path(__file__).parent / "reef_meta.json").read_text(encoding="utf-8"))
FIELDS: dict[str, str] = {f["name"]: f["type"] for f in META["fields"]}
TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}
RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}


class PageFetcher:
    """httpx client that rotates to a fresh proxy session when a request is blocked or fails."""

    def __init__(self, proxy_configuration) -> None:
        self.proxy_configuration = proxy_configuration
        self.client: httpx.AsyncClient | None = None

    async def _new_client(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        proxy = await self.proxy_configuration.new_url() if self.proxy_configuration else None
        self.client = httpx.AsyncClient(
            proxy=proxy, timeout=30, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8,*;q=0.5"},
        )

    async def get(self, url: str, attempts: int = 3) -> str | None:
        for attempt in range(1, attempts + 1):
            if self.client is None or attempt > 1:
                await self._new_client()
            try:
                resp = await self.client.get(url)
            except httpx.HTTPError as exc:
                Actor.log.warning(f"Request failed ({attempt}/{attempts}) {url}: {exc}")
                continue
            if resp.status_code == 200:
                return resp.text
            if resp.status_code not in RETRY_STATUSES:
                Actor.log.warning(f"HTTP {resp.status_code} for {url}, skipping")
                return None
            Actor.log.warning(f"HTTP {resp.status_code} ({attempt}/{attempts}) for {url}, rotating proxy")
        return None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def clean_item(raw: dict, page_url: str) -> dict:
    """Keep only declared fields and null out values of the wrong type, so every item matches the
    dataset schema users rely on."""
    item = {}
    for name, ftype in FIELDS.items():
        value = raw.get(name)
        item[name] = value if value is None or TYPE_CHECKS[ftype](value) else None
    if not item.get("url") and "url" in FIELDS:
        item["url"] = page_url
    return item


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        max_items = max(1, int(actor_input.get("maxItems") or 100))
        max_pages = max(1, int(actor_input.get("maxPages") or 20))
        save_html = bool(actor_input.get("debugSaveHtml"))
        params = {k: v for k, v in actor_input.items() if k not in RESERVED}

        start = extractor.start_urls(params)
        if not start:
            await Actor.fail(status_message="No start URLs could be built from the input. Check the input fields.")
            return

        proxy_configuration = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration"))
        fetcher = PageFetcher(proxy_configuration)

        queue = deque(start)
        queued = set(start)
        seen_items: set[str] = set()
        pushed = pages = parse_errors = fetch_errors = 0

        try:
            while queue and pushed < max_items and pages < max_pages:
                url = queue.popleft()
                html = await fetcher.get(url)
                if html is None:
                    fetch_errors += 1
                    continue
                pages += 1
                if save_html and pages <= 3:
                    await Actor.set_value(f"DEBUG_HTML_{pages}", html, content_type="text/html")
                    await Actor.set_value(f"DEBUG_URL_{pages}", {"url": url})
                try:
                    result = extractor.parse(html, url) or {}
                except Exception as exc:  # a broken page must not kill the whole run
                    parse_errors += 1
                    Actor.log.exception(f"Could not parse {url}: {exc}")
                    continue

                batch = []
                for raw in result.get("items") or []:
                    item = clean_item(raw, url)
                    key = json.dumps(item, sort_keys=True, default=str)
                    if key in seen_items:
                        continue
                    seen_items.add(key)
                    batch.append(item)
                    if pushed + len(batch) >= max_items:
                        break
                if batch:
                    charge = await Actor.push_data(batch)
                    pushed += len(batch)
                    if charge.event_charge_limit_reached:
                        Actor.log.info("Reached the maximum charge set for this run, stopping.")
                        break

                for nxt in result.get("next") or []:
                    nxt = urldefrag(nxt)[0]
                    if nxt and nxt not in queued:
                        queued.add(nxt)
                        queue.append(nxt)

                await Actor.set_status_message(f"Scraped {pushed} items from {pages} pages")
        finally:
            await fetcher.close()

        summary = f"Done: {pushed} items from {pages} pages"
        if fetch_errors or parse_errors:
            summary += f" ({fetch_errors} pages could not be fetched, {parse_errors} could not be parsed)"
        if pages == 0:
            await Actor.fail(status_message="Could not load any page. The site may be down or blocking; "
                                            "try enabling residential proxies.")
            return
        if pushed == 0 and parse_errors == pages:
            await Actor.fail(status_message="Pages loaded but none could be parsed. The site layout may have "
                                            "changed; the maintainer has been notified automatically.")
            return
        await Actor.set_status_message(summary, is_terminal=True)
