"""Command line: `python -m reef <command>`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import Config
from .context import Context
from .jobs import heal, prune, report, scout, spawn
from .scheduler import loop, tick


def doctor(ctx: Context, notify: bool) -> int:
    problems = 0
    missing = ctx.cfg.missing()
    if missing:
        print(f"[x] missing settings: {', '.join(missing)} (see .env.example)")
        return 1
    try:
        print(f"[ok] Apify token works - account '{ctx.apify.username()}'")
        usage = ctx.apify.monthly_usage_usd()
        print(f"[ok] Apify usage this month: {'unknown' if usage is None else f'${usage:.2f}'}")
    except Exception as exc:
        problems += 1
        print(f"[x] Apify: {exc}")
    try:
        free, paid = ctx.llm.models()  # type: ignore[attr-defined]
        print(f"[ok] OpenRouter free models: {', '.join(free) or 'NONE'}")
        print(f"[ok] OpenRouter paid fallback: {paid or 'none (free only)'}")
        if not free and not paid:
            problems += 1
        reply = ctx.llm.complete("Reply with the single word: pong", "ping", purpose="doctor", max_tokens=20,
                                 allow_paid=False)
        print(f"[ok] {reply.model} answered: {reply.text.strip()[:40]!r}")
    except Exception as exc:
        problems += 1
        print(f"[x] OpenRouter: {exc}")
    channels = [n for n, on in (("telegram", ctx.cfg.telegram_bot_token), ("email", ctx.cfg.smtp_host)) if on]
    print(f"[{'ok' if channels else '!'}] notification channels: {', '.join(channels) or 'none - reports only saved to disk'}")
    if notify and channels:
        ctx.notifier.send("Reef doctor", "Test message: notifications work.")
        print("     test message sent")
    print("All good." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


def status(ctx: Context) -> None:
    rows = ctx.state.actors()
    print(f"{'ACTOR':45} {'STATUS':12} {'USERS30':>7} {'HEALS':>5}  LAST OK")
    for r in rows:
        last_ok = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["last_ok_at"])) if r["last_ok_at"] else "-"
        print(f"{r['name'][:45]:45} {r['status']:12} {r['users30']:>7} {r['heal_count']:>5}  {last_ok}")
    print(f"\ncandidates: {len(ctx.state.candidates('new'))} queued")
    for e in ctx.state.events_since(time.time() - 3 * 86400)[-15:]:
        stamp = time.strftime("%m-%d %H:%M", time.gmtime(e["ts"]))
        print(f"  {stamp} {e['kind']:12} {e['actor'][:30]:30} {e['message'][:90]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reef", description="Autonomous fleet of paid Apify Actors.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run forever (what the Docker container does)")
    sub.add_parser("tick", help="run every due job once and exit (for cron)")
    d = sub.add_parser("doctor", help="check keys, models and notification channels")
    d.add_argument("--notify", action="store_true", help="also send a test notification")
    sub.add_parser("scout", help="find new candidate sites now")
    sub.add_parser("spawn", help="build and publish one Actor now")
    h = sub.add_parser("heal", help="check (and repair) Actors now")
    h.add_argument("--force", action="store_true", help="check every Actor, even if checked recently")
    sub.add_parser("prune", help="retire unused Actors and clone winners now")
    sub.add_parser("report", help="build and send the digest now")
    sub.add_parser("status", help="show the fleet")
    sub.add_parser("candidates", help="list candidate sites")
    sub.add_parser("pause", help="stop all jobs (stop switch)")
    sub.add_parser("resume", help="resume jobs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()

    if args.cmd == "pause":
        cfg.pause_file.write_text("paused\n")
        print(f"Paused. Delete {cfg.pause_file} or run `python -m reef resume` to continue.")
        return 0
    if args.cmd == "resume":
        cfg.pause_file.unlink(missing_ok=True)
        print("Resumed.")
        return 0
    if args.cmd not in ("status", "candidates") and cfg.missing():
        print(f"Missing settings: {', '.join(cfg.missing())}. Copy .env.example to .env and fill it in.")
        return 1

    ctx = Context.create(cfg)
    if args.cmd == "doctor":
        return doctor(ctx, args.notify)
    if args.cmd == "run":
        loop(ctx)
    elif args.cmd == "tick":
        print(tick(ctx))
    elif args.cmd == "scout":
        print(f"{scout.run(ctx)} new candidates")
    elif args.cmd == "spawn":
        print(spawn.run(ctx) or "nothing published (see `python -m reef status`)")
    elif args.cmd == "heal":
        print(heal.run(ctx, force=args.force))
    elif args.cmd == "prune":
        print(prune.run(ctx))
    elif args.cmd == "report":
        print(report.run(ctx))
    elif args.cmd == "status":
        status(ctx)
    elif args.cmd == "candidates":
        for c in ctx.state.candidates():
            print(json.dumps({k: c[k] for k in ("domain", "status", "score", "source", "reason")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
