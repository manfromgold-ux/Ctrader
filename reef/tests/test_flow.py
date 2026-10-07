"""End-to-end: scout -> spawn (real engine run) -> site redesign -> self-repair -> unfixable -> maintenance ->
prune -> report, with a scripted model and an Apify gateway that runs Actors locally."""
import json
import time

from fakes import EXTRACTOR_V1, EXTRACTOR_V2, FakeApify, FakeLLM, build_reply

from reef import scheduler
from reef.apify_gw import ActorStats, StoreHit
from reef.jobs import heal, prune, report, scout, spawn
from reef.state import DAY


def scout_reply(base):
    return "```json\n" + json.dumps([
        {"domain": "127.0.0.1", "start_url": f"{base}/tenders?q=roads&page=1", "data": "public tenders",
         "buyers": "construction firms", "search_terms": ["tenders"]},
        {"domain": "linkedin.com", "start_url": "https://linkedin.com/jobs", "data": "jobs", "buyers": "x"},
        {"domain": "bad", "start_url": "ftp://nope"},
    ]) + "\n```"


def heal_reply(base, version=EXTRACTOR_V2):
    return "```python\n" + version.format(base=base) + "\n```"


def test_full_lifecycle(site, make_ctx):
    llm = FakeLLM({"scout": [scout_reply(site.base)], "build": [build_reply(site.base)],
                   "heal": [heal_reply(site.base)]})
    apify = FakeApify()
    ctx = make_ctx(llm, apify)

    # Scout: one usable site, one blocked domain, one junk idea.
    assert scout.run(ctx) == 1
    statuses = {c["domain"]: c["status"] for c in ctx.state.candidates()}
    assert statuses == {"127.0.0.1": "new", "linkedin.com": "rejected"}

    # Spawn: builds, runs the real engine "on Apify", prices and publishes.
    name = spawn.run(ctx)
    assert name == "site-127-0-0-1-scraper"
    row = ctx.state.actor(name)
    assert row["status"] == "live" and apify.public[row["apify_id"]] and apify.priced[row["apify_id"]]
    canary = apify.runs[-1]
    assert canary["input"]["debugSaveHtml"] is True
    readme = next(f["content"] for f in apify.sources[row["apify_id"]] if f["name"] == "README.md")
    assert "Road resurfacing lot" in readme  # real sample output in the README

    # Healthy check: nothing to do.
    assert heal.run(ctx, force=True) == {"ok": 1}

    # The site is redesigned: the extractor breaks, Reef repairs it and ships v2.
    site.layout = "v2"
    assert heal.run(ctx, force=True) == {"healed": 1}
    row = ctx.state.actor(name)
    assert row["version"] == 2 and row["heal_count"] == 1 and "notice-card" in row["code"]
    heal_prompt = llm.calls[-1][1]
    assert "notice-card" in heal_prompt and '"value_eur"' in heal_prompt  # sees new HTML + field contract
    deployed = next(f["content"] for f in apify.sources[row["apify_id"]] if f["name"] == "src/extractor.py")
    assert "notice-card" in deployed

    # The site goes into a state the model cannot fix: Actor goes to maintenance, old code is restored.
    site.layout = "v3"
    llm.replies["heal"] = [heal_reply(site.base, EXTRACTOR_V1)]
    assert heal.run(ctx, force=True) == {"broken": 1}
    row = ctx.state.actor(name)
    assert row["status"] == "maintenance"
    assert any("is broken" in subject for subject, _ in ctx.notifier.sent)
    deployed = next(f["content"] for f in apify.sources[row["apify_id"]] if f["name"] == "src/extractor.py")
    assert "notice-card" in deployed  # rolled back to the last good code

    # The site comes back: the Actor recovers on its own.
    site.layout = "v2"
    assert heal.run(ctx, force=True) == {"ok": 1}
    assert ctx.state.actor(name)["status"] == "live"

    # Report summarises it all.
    text = report.run(ctx)
    assert "1 live" in text and "1 repaired" in text and "1 published" in text
    assert list(ctx.cfg.reports_dir.glob("*.txt"))


def test_spawn_feeds_back_failures_and_gives_up(site, make_ctx):
    broken = build_reply(site.base).replace("article.tender", "article.nothing-here")
    llm = FakeLLM({"build": [broken]})
    ctx = make_ctx(llm, FakeApify())
    ctx.state.add_candidate("127.0.0.1", f"{site.base}/tenders?q=roads&page=1", "tenders", "firms", 1.0)
    assert spawn.run(ctx) is None
    cand = ctx.state.candidates()[0]
    assert cand["status"] == "failed" and "expected at least 3 items" in cand["reason"]
    builds = [u for p, u in llm.calls if p == "build"]
    assert len(builds) == ctx.cfg.max_build_attempts
    assert "previous attempt failed" in builds[1]


def test_pending_when_pricing_api_refuses(site, make_ctx):
    apify = FakeApify()
    apify.pricing_ok = False
    ctx = make_ctx(FakeLLM({"build": [build_reply(site.base)]}), apify)
    ctx.state.add_candidate("127.0.0.1", f"{site.base}/tenders?q=roads&page=1", "tenders", "firms", 1.0)
    name = spawn.run(ctx)
    row = ctx.state.actor(name)
    assert row["status"] == "pending" and not apify.public.get(row["apify_id"])
    assert any("Action needed" in s for s, _ in ctx.notifier.sent)

    apify.pricing_ok = True  # e.g. the user set pricing in the console, or the API now accepts it
    heal.run(ctx, force=True)
    assert ctx.state.actor(name)["status"] == "live" and apify.public[row["apify_id"]]


def test_caps_and_strong_incumbents(site, make_ctx, cfg):
    hits = [StoreHit("127.0.0.1 Scraper", "x", "rival", "scrapes 127.0.0.1", users30=900, rating=4.8)]
    ctx = make_ctx(FakeLLM({"scout": [scout_reply(site.base)]}), FakeApify(store_hits=hits))
    assert scout.run(ctx) == 0
    assert "strong incumbent" in ctx.state.candidates()[0]["reason"]

    cfg.max_new_actors_per_week = 0
    assert "weekly cap" in spawn.blocked_reason(ctx)


def test_prune_retires_unused_and_clones_winners(site, make_ctx):
    llm = FakeLLM({"build": [build_reply(site.base)], "clone": ["```json\n[]\n```"]})
    apify = FakeApify()
    ctx = make_ctx(llm, apify)
    ctx.state.add_candidate("127.0.0.1", f"{site.base}/tenders?q=roads&page=1", "tenders", "firms", 1.0)
    name = spawn.run(ctx)
    actor_id = ctx.state.actor(name)["apify_id"]

    apify.stats_by_id[actor_id] = ActorStats(users30=25)
    assert prune.run(ctx) == {"retired": 0, "clone_candidates": 0}
    assert ctx.state.actor(name)["cloned"] == 1 and any(p == "clone" for p, _ in llm.calls)

    ctx.state.update_actor(name, published_at=time.time() - 90 * DAY)
    apify.stats_by_id[actor_id] = ActorStats(users30=1)
    assert prune.run(ctx)["retired"] == 1
    assert ctx.state.actor(name)["status"] == "retired" and actor_id in apify.retired


def test_scheduler_pause_and_crash_isolation(make_ctx, monkeypatch):
    ctx = make_ctx(FakeLLM({}), FakeApify())
    ctx.cfg.pause_file.write_text("x")
    assert scheduler.tick(ctx) == {}
    ctx.cfg.pause_file.unlink()

    def boom(_ctx):
        raise RuntimeError("kaput")

    monkeypatch.setattr(scheduler.heal, "run", boom)
    ran = scheduler.tick(ctx)
    assert isinstance(ran["heal"], RuntimeError)
    assert "report" in ran  # other jobs still ran
    assert any("crashed" in s for s, _ in ctx.notifier.sent)
    assert scheduler.tick(ctx) == {}  # nothing due right after


def test_spawn_survives_apify_api_errors(site, make_ctx):
    apify = FakeApify()

    def refuse(actor_id):
        raise RuntimeError("API said no")

    apify.build = refuse
    ctx = make_ctx(FakeLLM({"build": [build_reply(site.base)]}), apify)
    ctx.state.add_candidate("127.0.0.1", f"{site.base}/tenders?q=roads&page=1", "tenders", "firms", 1.0)
    assert spawn.run(ctx) is None
    cand = ctx.state.candidates()[0]
    assert cand["status"] == "failed" and "API said no" in cand["reason"]
    assert apify.deleted == ["act1"]  # half-created Actor cleaned up
