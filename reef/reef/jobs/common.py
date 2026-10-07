"""Shared steps: test an extractor against the live site, and ship a version to Apify with a canary run."""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import actor_pkg, sandbox
from ..apify_gw import VERSION
from ..context import Context
from ..fetch import same_site, trim_html
from ..spec import ActorSpec
from ..validate import Report, check_items

CANARY_MAX_ITEMS = 25


@dataclass
class Check:
    ok: bool
    failure: str = ""
    items: list[dict] = field(default_factory=list)
    pages: list[tuple[str, str]] = field(default_factory=list)  # (url, html) the check looked at
    blocked: bool = False  # the site refused us (not the extractor's fault)
    report: Report | None = None

    def prompt_page(self, max_chars: int) -> tuple[str, str]:
        if not self.pages:
            return "", ""
        url, page_html = self.pages[0]
        return url, trim_html(page_html, max_chars)


def local_check(ctx: Context, spec: ActorSpec, code: str) -> Check:
    """Run the extractor in the sandbox against pages fetched by Reef itself."""
    res = sandbox.run(code, "start_urls", {"params": spec.test_params})
    if not res.ok:
        return Check(ok=False, failure=f"start_urls() failed: {res.error}\n" + "\n".join(res.logs[-6:]))
    urls = [u for u in res.data if same_site(u, spec.domain)]
    if not urls:
        return Check(ok=False, failure=f"start_urls() returned no URLs on {spec.domain}: {res.data[:3]}")

    fetched = ctx.fetcher.get(urls[0])
    if not fetched.ok:
        blocked = fetched.blocked or fetched.status in (0, 401, 403, 429)
        # A 404/500 page is still useful context for a repair (e.g. the URL scheme changed).
        pages = [] if blocked or not fetched.html else [(fetched.final_url or urls[0], fetched.html)]
        return Check(ok=False, failure=f"could not load {urls[0]}: {fetched.describe()}", blocked=blocked,
                     pages=pages)
    return offline_check(spec, code, [(fetched.final_url or urls[0], fetched.html)])


def offline_check(spec: ActorSpec, code: str, pages: list[tuple[str, str]]) -> Check:
    """Parse already-fetched pages in the sandbox and check the output contract."""
    res = sandbox.run(code, "parse", {"pages": [{"url": u, "html": h} for u, h in pages]})
    if not res.ok:
        return Check(ok=False, failure=f"parse() crashed: {res.error}\n" + "\n".join(res.logs[-6:]), pages=pages)
    items = [it for page in res.data for it in page["items"]]
    report = check_items(spec, items)
    if not report.ok:
        return Check(ok=False, failure=f"output check failed: {report.summary()}\nsample items: {items[:2]}",
                     items=items, pages=pages, report=report)
    return Check(ok=True, items=items, pages=pages, report=report)


def apify_check(ctx: Context, actor_id: str, spec: ActorSpec) -> Check:
    """Canary run on Apify itself (real proxies, real build). Saves page HTML for healing."""
    run_input = {**spec.test_params, "maxItems": CANARY_MAX_ITEMS, "maxPages": 2, "debugSaveHtml": True,
                 "proxyConfiguration": {"useApifyProxy": True}}
    result = ctx.apify.run(actor_id, run_input, max_items=CANARY_MAX_ITEMS)
    if not result.succeeded:
        return Check(ok=False, failure=f"Apify run {result.status}: {result.status_message}",
                     pages=result.debug_pages)
    report = check_items(spec, result.items)
    if not report.ok:
        return Check(ok=False, failure=f"Apify run output check failed: {report.summary()}",
                     items=result.items, pages=result.debug_pages, report=report)
    return Check(ok=True, items=result.items, pages=result.debug_pages, report=report)


def ship(ctx: Context, spec: ActorSpec, code: str, sample_items: list[dict], actor_id: str = "") -> tuple[str, Check]:
    """Upload source, build, and canary-run on Apify. Returns (actor_id, check)."""
    # Apify builds version "0.0" every time; Reef's own revision counter lives in its database.
    files = actor_pkg.source_files(spec, code, sample_items, version=VERSION,
                                   price_per_1000=ctx.cfg.price_per_1000_results)
    actor_id = ctx.apify.deploy(spec, files, actor_id=actor_id)
    try:  # from here on the Actor exists, so always hand its id back for cleanup or rollback
        built, build_log = ctx.apify.build(actor_id)
        if not built:
            return actor_id, Check(ok=False, failure=build_log)
        return actor_id, apify_check(ctx, actor_id, spec)
    except Exception as exc:
        return actor_id, Check(ok=False, failure=f"Apify API error: {type(exc).__name__}: {exc}")
