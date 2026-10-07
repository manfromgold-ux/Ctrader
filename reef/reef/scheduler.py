"""The forever loop: runs each job when it is due, survives crashes, honours the stop switch."""
from __future__ import annotations

import logging
import time
from typing import Callable

from .context import Context
from .jobs import Retry, heal, prune, report, scout, spawn

log = logging.getLogger("reef.scheduler")
MIN_CANDIDATE_QUEUE = 8
ALERT_COOLDOWN_S = 12 * 3600


def _scout_if_needed(ctx: Context):
    if len(ctx.state.candidates("new")) >= MIN_CANDIDATE_QUEUE:
        return "queue full"
    return scout.run(ctx)


def jobs(ctx: Context) -> list[tuple[str, float, Callable[[Context], object]]]:
    cfg = ctx.cfg
    return [
        ("heal", cfg.heal_interval_hours, heal.run),
        ("scout", cfg.scout_interval_hours, _scout_if_needed),
        ("spawn", cfg.spawn_interval_hours, spawn.run),
        ("prune", cfg.prune_interval_hours, prune.run),
        ("report", cfg.report_interval_hours, report.run),
    ]


def paused(ctx: Context) -> bool:
    return ctx.cfg.pause_file.exists()


def tick(ctx: Context, now: float | None = None) -> dict[str, object]:
    """Run every job that is due. Returns {job: result} for the jobs that ran."""
    ran: dict[str, object] = {}
    if paused(ctx):
        log.info("paused (remove %s to resume)", ctx.cfg.pause_file)
        return ran
    for name, hours, fn in jobs(ctx):
        t = time.time() if now is None else now
        if t - ctx.state.get(f"last_run:{name}", 0) < hours * 3600:
            continue
        ctx.state.put(f"last_run:{name}", t)
        log.info("running %s", name)
        try:
            ran[name] = fn(ctx)
            log.info("%s -> %s", name, ran[name] if name != "report" else "sent")
            if isinstance(ran[name], Retry):  # due again after the retry delay, not the full interval
                ctx.state.put(f"last_run:{name}", t - hours * 3600 + ran[name].hours * 3600)
        except Exception as exc:  # one failing job must never stop the others
            log.exception("%s crashed", name)
            ctx.state.log("error", f"{name} crashed: {type(exc).__name__}: {exc}")
            ran[name] = exc
            if t - ctx.state.get(f"last_alert:{name}", 0) > ALERT_COOLDOWN_S:
                ctx.state.put(f"last_alert:{name}", t)
                ctx.notifier.send(f"Job '{name}' crashed", f"{type(exc).__name__}: {exc}\nReef keeps running.")
    return ran


def loop(ctx: Context, sleep_s: float = 300) -> None:  # pragma: no cover - infinite loop
    log.info("Reef started; checking for due jobs every %.0fs", sleep_s)
    while True:
        tick(ctx)
        time.sleep(sleep_s)
