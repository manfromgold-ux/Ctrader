"""Heal: canary-test every Actor, repair broken extractors, and roll back anything that does not pass.

A repair ships only if it keeps the exact output contract, passes the local check AND a canary run on
Apify. Otherwise the previous code is restored and the Actor goes into maintenance (you get an alert).
"""
from __future__ import annotations

import json
import logging
import time

from .. import actor_pkg
from ..apify_gw import VERSION
from ..context import Context
from ..fetch import trim_html
from ..llm import LLMError, extract_code
from ..prompts import HEAL_SYSTEM, heal_user
from ..spec import ActorSpec
from ..state import DAY
from .common import Check, apify_check, local_check, offline_check, ship

log = logging.getLogger("reef.heal")
PUBLIC_FAILURE_RATE_ALARM = 0.3
PUBLIC_FAILURE_MIN_RUNS = 8


def refresh_stats(ctx: Context, row) -> object | None:
    try:
        stats = ctx.apify.stats(row["apify_id"])
    except Exception as exc:
        log.warning("stats unavailable for %s: %s", row["name"], exc)
        return None
    ctx.state.update_actor(row["name"], users7=stats.users7, users30=stats.users30, runs30_total=stats.runs30_total,
                           runs30_failed=stats.runs30_failed, rating=stats.rating)
    return stats


def diagnose(ctx: Context, row, spec: ActorSpec, stats) -> Check:
    check = local_check(ctx, spec, row["code"])
    if check.blocked:
        # Our server is refused but Apify's proxies may get through: let the platform decide.
        return apify_check(ctx, row["apify_id"], spec)
    if check.ok and stats and stats.runs30_total >= PUBLIC_FAILURE_MIN_RUNS:
        rate = stats.runs30_failed / stats.runs30_total
        if rate >= PUBLIC_FAILURE_RATE_ALARM:
            on_apify = apify_check(ctx, row["apify_id"], spec)
            if not on_apify.ok:
                on_apify.failure = f"users see {rate:.0%} failed runs; " + on_apify.failure
                return on_apify
    return check


def repair(ctx: Context, row, spec: ActorSpec, check: Check) -> tuple[str, str]:
    """Try to fix the extractor. Returns (outcome, last failure); outcome is healed | transient | failed."""
    failure, pages, code = check.failure, list(check.pages), row["code"]
    if not pages:
        probe = apify_check(ctx, row["apify_id"], spec)
        if probe.ok:
            return "transient", ""  # the problem was on our side (e.g. our IP); the Actor works on Apify
        pages, failure = probe.pages, f"{failure}\nApify canary: {probe.failure}"
    if not pages:
        return "failed", f"{failure}\n(no page HTML available to repair against)"

    deployed_new = False
    for attempt in range(ctx.cfg.max_heal_attempts):
        url, page_html = pages[0]
        try:
            reply = ctx.llm.complete(
                HEAL_SYSTEM, heal_user(spec.to_dict(), code, failure, url, trim_html(page_html, ctx.cfg.html_prompt_chars)),
                purpose="heal", max_tokens=16000, prefer_paid=attempt > 0)
        except LLMError as exc:
            failure = f"LLM unavailable: {exc}"
            break
        candidate = extract_code(reply.text)
        if not candidate:
            failure = "model reply had no ```python block"
            continue
        test = local_check(ctx, spec, candidate)
        if test.blocked:
            test = offline_check(spec, candidate, pages)
        if not test.ok:  # next attempt starts from this candidate and its specific problem
            failure, code = test.failure, candidate
            if test.pages:
                pages = test.pages
            continue
        deployed_new = True
        try:
            _, canary = ship(ctx, spec, candidate, test.items, actor_id=row["apify_id"])
        except Exception as exc:
            failure = f"Apify API error while shipping the fix: {type(exc).__name__}: {exc}"
            break
        if canary.ok:
            ctx.state.update_actor(row["name"], code=candidate, version=row["version"] + 1,
                                   heal_count=row["heal_count"] + 1,
                                   sample_json=json.dumps(test.items[:3], default=str))
            return "healed", ""
        failure, code = canary.failure, candidate
        if canary.pages:
            pages = canary.pages

    if deployed_new:  # put the last known code back so users are not left on an untested build
        try:
            files = actor_pkg.source_files(spec, row["code"], json.loads(row["sample_json"]), version=VERSION,
                                           price_per_1000=ctx.cfg.price_per_1000_results)
            ctx.apify.deploy(spec, files, actor_id=row["apify_id"])
            ctx.apify.build(row["apify_id"])
        except Exception as exc:
            log.error("rollback of %s failed: %s", row["name"], exc)
    return "failed", failure


def handle_pending(ctx: Context, row) -> None:
    """Actors that were built but not priced/published: finish the job once it becomes possible."""
    spec = ActorSpec.from_dict(json.loads(row["spec_json"]))
    stats = refresh_stats(ctx, row)
    priced = bool(stats and stats.has_pricing)
    if not priced:
        priced, _ = ctx.apify.set_pricing(row["apify_id"], ctx.cfg.price_per_1000_results)
    if priced:
        published, _ = ctx.apify.publish(row["apify_id"], spec)
        if published:
            ctx.state.update_actor(row["name"], status="live", published_at=time.time())
            ctx.state.log("spawned", "published after pending step was resolved", actor=row["name"])


def check_actor(ctx: Context, row) -> str:
    name, now = row["name"], time.time()
    spec = ActorSpec.from_dict(json.loads(row["spec_json"]))
    stats = refresh_stats(ctx, row)
    check = diagnose(ctx, row, spec, stats)
    ctx.state.update_actor(name, last_check_at=now)

    if check.ok:
        ctx.state.update_actor(name, last_ok_at=now, consecutive_failures=0, status="live", maintenance_since=None)
        if row["status"] == "maintenance":
            ctx.state.log("recovered", "passing again", actor=name)
        return "ok"

    outcome, failure = repair(ctx, row, spec, check)
    if outcome in ("healed", "transient"):
        ctx.state.update_actor(name, last_ok_at=time.time(), consecutive_failures=0, status="live",
                               maintenance_since=None)
        if outcome == "healed":
            ctx.state.log("healed", f"repaired after: {check.failure[:300]}", actor=name)
        return outcome

    since = row["maintenance_since"] or now
    ctx.state.update_actor(name, status="maintenance", maintenance_since=since,
                           consecutive_failures=row["consecutive_failures"] + 1)
    ctx.state.log("broken", failure[:1000], actor=name)
    if row["status"] != "maintenance":
        ctx.notifier.send(f"{name} is broken", f"Automatic repair failed. Reef keeps retrying every "
                          f"{ctx.cfg.heal_interval_hours:g}h and retires it after "
                          f"{ctx.cfg.retire_after_maintenance_days} days.\n\nLast problem:\n{failure[:1500]}")
    if now - since >= ctx.cfg.retire_after_maintenance_days * DAY:
        ctx.apify.retire(row["apify_id"])
        ctx.state.update_actor(name, status="retired")
        ctx.state.log("retired", "broken for too long", actor=name)
        return "retired"
    return "broken"


def run(ctx: Context, force: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    interval = ctx.cfg.heal_interval_hours * 3600
    for row in ctx.state.actors("pending"):
        try:
            handle_pending(ctx, row)
        except Exception as exc:
            ctx.state.log("error", f"pending step crashed: {exc}", actor=row["name"])
    for row in ctx.state.actors("live", "maintenance"):
        if not force and row["last_check_at"] and time.time() - row["last_check_at"] < interval * 0.9:
            continue
        try:
            outcome = check_actor(ctx, row)
        except Exception as exc:
            log.exception("heal crashed on %s", row["name"])
            ctx.state.log("error", f"heal crashed: {exc}", actor=row["name"])
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
