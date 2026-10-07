"""Report: the one-screen weekly digest - the only thing you need to read."""
from __future__ import annotations

import time

from ..context import Context
from ..state import DAY, month_start


def build(ctx: Context) -> str:
    state, cfg, now = ctx.state, ctx.cfg, time.time()
    week = now - 7 * DAY
    actors = state.actors()
    by_status: dict[str, list] = {}
    for a in actors:
        by_status.setdefault(a["status"], []).append(a)
    live = by_status.get("live", [])
    events = state.events_since(week)
    count = lambda kind: sum(1 for e in events if e["kind"] == kind)  # noqa: E731
    users30 = sum(a["users30"] for a in live)
    top = sorted(live, key=lambda a: a["users30"], reverse=True)[:5]
    usage = ctx.apify.monthly_usage_usd()
    llm_month = state.llm_spend_since(month_start())

    lines = [
        f"Reef weekly report - {time.strftime('%Y-%m-%d', time.gmtime(now))}",
        "",
        f"Fleet: {len(live)} live, {len(by_status.get('maintenance', []))} broken, "
        f"{len(by_status.get('pending', []))} waiting on you, {len(by_status.get('retired', []))} retired",
        f"Active users (30 days): {users30} across the fleet",
        f"This week: {count('spawned')} published, {count('healed')} repaired, {count('retired')} retired, "
        f"{count('build_failed')} build attempts failed",
        f"Spend this month: LLM ${llm_month:.2f} of ${cfg.llm_monthly_budget_usd:.2f}; "
        + (f"Apify ${usage:.2f} of ${cfg.apify_monthly_cap_usd:.2f}" if usage is not None else "Apify usage unknown"),
        f"Candidate queue: {len(state.candidates('new'))} sites waiting to be built",
    ]
    if top:
        lines += ["", "Top Actors by users (30d):"]
        lines += [f"  {a['users30']:>4}  {a['name']}  (repaired {a['heal_count']}x)" for a in top]
    pending = by_status.get("pending", [])
    if pending:
        lines += ["", "Needs you (pricing/publishing could not be done via API):"]
        lines += [f"  - {a['name']}: https://console.apify.com/actors/{a['apify_id']}" for a in pending]
    broken = by_status.get("maintenance", [])
    if broken:
        lines += ["", "Broken (auto-repair keeps trying):"]
        for a in broken:
            since = time.strftime("%Y-%m-%d", time.gmtime(a["maintenance_since"] or now))
            last = next((e["message"] for e in reversed(events) if e["actor"] == a["name"] and e["kind"] == "broken"), "")
            lines.append(f"  - {a['name']} since {since}: {last[:160]}")
    errors = [e for e in events if e["kind"] == "error"]
    if errors:
        lines += ["", f"Errors this week: {len(errors)} (latest: {errors[-1]['message'][:160]})"]
    lines += [
        "",
        "Revenue: Reef tracks users, not dollars - check payouts in Apify Console.",
        "Stop switch: run `python -m reef pause` (or create data/PAUSE). `python -m reef resume` restarts.",
    ]
    return "\n".join(lines)


def run(ctx: Context) -> str:
    text = build(ctx)
    ctx.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.cfg.reports_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.txt"
    path.write_text(text, encoding="utf-8")
    ctx.notifier.send("Weekly report", text)
    return text
