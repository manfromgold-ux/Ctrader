"""Spawn: turn the best candidate into a tested, priced, published Actor."""
from __future__ import annotations

import logging
import re
import time

from ..context import Context
from ..fetch import trim_html
from ..llm import LLMError, extract_block, extract_json
from ..prompts import BUILD_SYSTEM, build_user
from ..spec import ActorSpec, SpecError, normalize
from ..state import DAY
from . import Retry
from .common import Check, local_check, ship

log = logging.getLogger("reef.spawn")


def blocked_reason(ctx: Context) -> str:
    cfg, state = ctx.cfg, ctx.state
    made = state.actors_created_since(time.time() - 7 * DAY)
    if made >= cfg.max_new_actors_per_week:
        return f"weekly cap reached ({made}/{cfg.max_new_actors_per_week} new Actors in 7 days)"
    active = len(state.actors("live", "pending", "maintenance"))
    if active >= cfg.max_live_actors:
        return f"fleet is full ({active}/{cfg.max_live_actors} Actors)"
    usage = ctx.apify.monthly_usage_usd()
    if usage is not None and usage >= cfg.apify_monthly_cap_usd:
        return f"Apify spend ${usage:.2f} reached the monthly cap ${cfg.apify_monthly_cap_usd:.2f}"
    return ""


def _unique_name(ctx: Context, spec: ActorSpec) -> None:
    base, n = spec.name, 2
    while ctx.state.actor(spec.name) is not None:
        spec.name = f"{base[:50]}-{n}"
        n += 1


def _error_context(failure: str, code: str) -> str:
    """For a syntax error, show the model the exact lines around it (the code shown later may be cut)."""
    m = re.search(r"SyntaxError.*?\(line (\d+)\)", failure)
    if not m:
        return ""
    n, lines = int(m.group(1)), code.splitlines()
    snippet = "\n".join(f"{i:>4}: {lines[i - 1]}" for i in range(max(1, n - 3), min(len(lines), n + 2) + 1))
    return f"\n\nThe error is here:\n{snippet}\nIf your code was cut off, write a shorter module."


def design(ctx: Context, cand: dict, page_url: str, page_html: str) -> tuple[ActorSpec, str, Check] | str:
    """Ask the model for a spec + extractor and iterate on test feedback. Returns the result or a failure."""
    feedback = ""
    for attempt in range(ctx.cfg.max_build_attempts):
        try:
            reply = ctx.llm.complete(BUILD_SYSTEM,
                                     build_user(cand, page_url, trim_html(page_html, ctx.cfg.html_prompt_chars), feedback),
                                     purpose="build", max_tokens=16000, prefer_paid=attempt > 0)
        except LLMError as exc:
            return f"LLM unavailable: {exc}"
        raw_spec, code = extract_json(reply.text, "json"), extract_block(reply.text, "python")
        if not isinstance(raw_spec, dict) or not code:
            feedback = "Your answer must contain one ```json block (the spec object) and one ```python block."
            continue
        try:
            spec = normalize(raw_spec, cand["domain"])
        except SpecError as exc:
            feedback = f"Spec problems: {exc}"
            continue
        check = local_check(ctx, spec, code)
        if check.ok:
            return spec, code, check
        if check.blocked:
            return f"site blocks automated access: {check.failure}"
        feedback = (f"{check.failure[:3000]}{_error_context(check.failure, code)}\n\n"
                    f"Your previous extractor was:\n```python\n{code[:6000]}\n```")
        if check.pages and check.pages[0][0] != page_url:
            page_url, page_html = check.pages[0]
        log.info("build attempt %d for %s failed: %s", attempt + 1, cand["domain"], feedback[:200])
    return f"no working extractor after {ctx.cfg.max_build_attempts} attempts; last problem: {feedback[:600]}"


def build_candidate(ctx: Context, cand: dict) -> str | None:
    state, apify, cfg = ctx.state, ctx.apify, ctx.cfg
    state.update_candidate(cand["id"], status="building", attempts=cand["attempts"] + 1)
    page = ctx.fetcher.get(cand["start_url"])
    if not page.ok:
        state.update_candidate(cand["id"], status="rejected", reason=f"start page: {page.describe()}")
        return None

    designed = design(ctx, cand, page.final_url or cand["start_url"], page.html)
    if isinstance(designed, str) and designed.startswith("LLM unavailable") and cand["attempts"] < 2:
        state.update_candidate(cand["id"], status="new", reason=designed[:300])  # model outage: try again later
        return None
    if isinstance(designed, str):
        state.update_candidate(cand["id"], status="failed", reason=designed)
        state.log("build_failed", f"{cand['domain']}: {designed[:300]}")
        return None
    spec, code, check = designed
    _unique_name(ctx, spec)

    actor_id = ""
    try:
        actor_id, canary = ship(ctx, spec, code, check.items)
    except Exception as exc:  # API refused the upload/build/run: record it instead of leaving it half-done
        canary = Check(ok=False, failure=f"Apify API error: {type(exc).__name__}: {exc}")
    if not canary.ok:
        if actor_id:
            try:
                apify.delete(actor_id)
            except Exception as exc:
                log.warning("could not delete failed Actor %s: %s", actor_id, exc)
        state.update_candidate(cand["id"], status="failed", reason=f"Apify canary: {canary.failure[:400]}")
        state.log("build_failed", f"{spec.name}: Apify canary failed: {canary.failure[:300]}")
        return None

    priced, price_err = apify.set_pricing(actor_id, cfg.price_per_1000_results)
    published, pub_err = apify.publish(actor_id, spec) if priced else (False, "")
    status = "live" if published else "pending"
    state.add_actor(spec.name, actor_id, spec.domain, status, spec.to_dict(), code, sample=check.items)
    state.update_candidate(cand["id"], status="built")
    if published:
        state.log("spawned", f"published {spec.title} ({canary.report.count} items in canary)", actor=spec.name)
    else:
        why = f"pricing: {price_err}" if not priced else f"publishing: {pub_err}"
        state.log("pending", f"built but not published ({why})", actor=spec.name)
        ctx.notifier.send(
            f"Action needed: {spec.name}",
            f"{spec.title} is built and tested but Reef could not finish {why}.\n"
            f"Open https://console.apify.com/actors/{actor_id} -> Publication, set pay-per-result pricing "
            f"(${cfg.price_per_1000_results:.2f}/1,000 results) and publish. Reef notices and takes over again.",
        )
    return spec.name


def run(ctx: Context) -> str | None | Retry:
    reason = blocked_reason(ctx)
    if reason:
        log.info("spawn skipped: %s", reason)
        return None
    queue = ctx.state.candidates("new")
    if not queue:
        return Retry(0.5, "no candidates yet")
    for cand in queue[:3]:
        try:
            name = build_candidate(ctx, dict(cand))
        except Exception as exc:
            log.exception("build of %s crashed", cand["domain"])
            ctx.state.update_candidate(cand["id"], status="failed", reason=f"crashed: {exc}"[:500])
            ctx.state.log("error", f"spawn crashed on {cand['domain']}: {exc}")
            continue
        if name:
            return name
    if ctx.state.candidates("new"):
        return Retry(3, "no build succeeded yet; more candidates waiting")
    return None
