"""Prune: retire Actors nobody uses after a fair trial, and copy winners to sibling sites."""
from __future__ import annotations

import json
import logging
import time

from ..context import Context
from ..llm import LLMError, extract_json
from ..prompts import CLONE_SYSTEM, clone_user
from ..state import DAY
from .heal import refresh_stats
from .scout import _ideas, evaluate

log = logging.getLogger("reef.prune")


def clone_winner(ctx: Context, row) -> int:
    spec = json.loads(row["spec_json"])
    try:
        reply = ctx.llm.complete(CLONE_SYSTEM, clone_user(spec, row["users30"], 5, sorted(ctx.state.known_domains()),
                                                          ctx.cfg.blocked_domains),
                                 purpose="clone", max_tokens=3000, allow_paid=False)
    except LLMError as exc:
        ctx.state.log("error", f"clone: {exc}", actor=row["name"])
        return 0
    added = sum(1 for idea in _ideas(extract_json(reply.text)) if evaluate(ctx, idea, source="clone") == "new")
    ctx.state.update_actor(row["name"], cloned=1)
    ctx.state.log("cloned", f"{added} sibling candidates queued", actor=row["name"])
    return added


def run(ctx: Context) -> dict[str, int]:
    cfg, now = ctx.cfg, time.time()
    retired = cloned = 0
    for row in ctx.state.actors("live"):
        stats = refresh_stats(ctx, row)
        if stats is None:
            continue
        age_days = (now - (row["published_at"] or row["created_at"])) / DAY
        # users30 counts the owner too (Reef's own canary runs), so <= 1 means no customers at all.
        if age_days >= cfg.min_age_days_before_prune and stats.users30 <= 1:
            ctx.apify.retire(row["apify_id"])
            ctx.state.update_actor(row["name"], status="retired")
            ctx.state.log("retired", f"no users after {age_days:.0f} days", actor=row["name"])
            retired += 1
        elif stats.users30 >= cfg.clone_threshold_users30 and not row["cloned"]:
            cloned += clone_winner(ctx, ctx.state.actor(row["name"]))
    return {"retired": retired, "clone_candidates": cloned}
