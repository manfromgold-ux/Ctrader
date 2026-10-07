"""Thin gateway over the Apify API. Everything Reef does on Apify goes through the ApifyGateway protocol,
so tests can swap in a fake that runs Actors locally."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

import httpx
from apify_client import ApifyClient

from .spec import ActorSpec

log = logging.getLogger("reef.apify")
VERSION = "0.0"
DATASET_ITEM_EVENT = "apify-default-dataset-item"


@dataclass
class StoreHit:
    title: str
    name: str
    username: str
    description: str
    users30: int
    rating: float | None


@dataclass
class RunResult:
    status: str
    items: list[dict] = field(default_factory=list)
    debug_pages: list[tuple[str, str]] = field(default_factory=list)  # (url, html)
    status_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


@dataclass
class ActorStats:
    users7: int = 0
    users30: int = 0
    runs30_total: int = 0
    runs30_failed: int = 0
    rating: float | None = None
    has_pricing: bool = False


class ApifyGateway(Protocol):
    def username(self) -> str: ...
    def store_search(self, query: str, limit: int = 10) -> list[StoreHit]: ...
    def deploy(self, spec: ActorSpec, files: list[dict], actor_id: str = "") -> str: ...
    def build(self, actor_id: str) -> tuple[bool, str]: ...
    def run(self, actor_id: str, run_input: dict, max_items: int, timeout_s: int = 300) -> RunResult: ...
    def set_pricing(self, actor_id: str, price_per_1000: float) -> tuple[bool, str]: ...
    def publish(self, actor_id: str, spec: ActorSpec) -> tuple[bool, str]: ...
    def retire(self, actor_id: str) -> None: ...
    def delete(self, actor_id: str) -> None: ...
    def stats(self, actor_id: str) -> ActorStats: ...
    def monthly_usage_usd(self) -> float | None: ...


class RealApify:
    def __init__(self, token: str):
        self.token = token
        self.client = ApifyClient(token)
        self._http = httpx.Client(base_url="https://api.apify.com/v2", timeout=60,
                                  headers={"Authorization": f"Bearer {token}"})
        self._username = ""

    def username(self) -> str:
        if not self._username:
            me = self.client.user().get()
            self._username = getattr(me, "username", "") or ""
        return self._username

    def store_search(self, query: str, limit: int = 10) -> list[StoreHit]:
        page = self.client.store().list(search=query, limit=limit)
        return [
            StoreHit(title=a.title, name=a.name, username=a.username, description=a.description or "",
                     users30=(a.stats.total_users30_days or 0) if a.stats else 0, rating=a.actor_review_rating)
            for a in page.items
        ]

    def deploy(self, spec: ActorSpec, files: list[dict], actor_id: str = "") -> str:
        version = {"versionNumber": VERSION, "sourceType": "SOURCE_FILES", "buildTag": "latest", "sourceFiles": files}
        if actor_id:
            self.client.actor(actor_id).version(VERSION).update(source_type="SOURCE_FILES", source_files=files,
                                                                 build_tag="latest")
            return actor_id
        actor = self.client.actors().create(
            name=spec.name, title=spec.title, description=spec.description, versions=[version],
            is_public=False, default_run_build="latest", default_run_memory_mbytes=512,
            default_run_timeout=timedelta(hours=1),
            example_run_input_body={**spec.test_params, "maxItems": 20},
            example_run_input_content_type="application/json",
        )
        return actor.id

    def build(self, actor_id: str) -> tuple[bool, str]:
        build = self.client.actor(actor_id).build(version_number=VERSION, wait_for_finish=60)
        if build.status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            build = self.client.build(build.id).wait_for_finish(wait_duration=timedelta(minutes=10)) or build
        if build.status == "SUCCEEDED":
            return True, ""
        log_text = self.client.log(build.id).get() or ""
        return False, f"build {build.status}: " + log_text[-1500:]

    def run(self, actor_id: str, run_input: dict, max_items: int, timeout_s: int = 300) -> RunResult:
        run = self.client.actor(actor_id).call(
            run_input=run_input, max_items=max_items, memory_mbytes=512,
            run_timeout=timedelta(seconds=timeout_s), wait_duration=timedelta(seconds=timeout_s + 120),
            logger=None,
        )
        if run is None:
            return RunResult(status="UNKNOWN")
        items = self.client.dataset(run.default_dataset_id).list_items(limit=max_items, clean=True).items
        pages: list[tuple[str, str]] = []
        if run_input.get("debugSaveHtml"):
            store = self.client.key_value_store(run.default_key_value_store_id)
            for i in range(1, 4):
                rec = store.get_record(f"DEBUG_HTML_{i}")
                if not rec:
                    break
                url_rec = store.get_record(f"DEBUG_URL_{i}") or {}
                url = (url_rec.get("value") or {}).get("url", "")
                value = rec.get("value")
                pages.append((url, value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)))
        return RunResult(status=run.status, items=list(items), debug_pages=pages,
                         status_message=getattr(run, "status_message", "") or "")

    def set_pricing(self, actor_id: str, price_per_1000: float) -> tuple[bool, str]:
        # Raw request: the typed client insists on platform-managed fields (createdAt, apifyMarginPercentage).
        body = {"pricingInfos": [{
            "pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {"actorChargeEvents": {DATASET_ITEM_EVENT: {
                "eventTitle": "Result",
                "eventDescription": "One extracted record in the dataset",
                "eventPriceUsd": round(price_per_1000 / 1000, 6),
                "isPrimaryEvent": True,
            }}},
        }]}
        resp = self._http.put(f"/acts/{actor_id}", json=body)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return True, ""

    def publish(self, actor_id: str, spec: ActorSpec) -> tuple[bool, str]:
        try:
            self.client.actor(actor_id).update(is_public=True, title=spec.title, description=spec.description,
                                               seo_title=spec.seo_title, seo_description=spec.seo_description,
                                               categories=spec.categories)
            return True, ""
        except Exception as exc:  # retry once without categories, the most common validation failure
            log.warning("publish with categories failed: %s", exc)
            try:
                self.client.actor(actor_id).update(is_public=True, title=spec.title, description=spec.description,
                                                   seo_title=spec.seo_title, seo_description=spec.seo_description)
                return True, ""
            except Exception as exc2:
                return False, str(exc2)[:300]

    def retire(self, actor_id: str) -> None:
        self.client.actor(actor_id).update(is_public=False, is_deprecated=True)

    def delete(self, actor_id: str) -> None:
        self.client.actor(actor_id).delete()

    def stats(self, actor_id: str) -> ActorStats:
        actor = self.client.actor(actor_id).get()
        if actor is None or actor.stats is None:
            return ActorStats()
        s = actor.stats
        runs = s.public_actor_run_stats30_days
        pricing = [p for p in (getattr(actor, "pricing_infos", None) or [])
                   if getattr(p, "pricing_model", "FREE") != "FREE"]
        return ActorStats(
            users7=s.total_users7_days or 0, users30=s.total_users30_days or 0,
            runs30_total=(runs.total or 0) if runs else 0,
            runs30_failed=((runs.failed or 0) + (runs.timed_out or 0)) if runs else 0,
            rating=s.actor_review_rating, has_pricing=bool(pricing),
        )

    def monthly_usage_usd(self) -> float | None:
        try:
            limits = self.client.user().limits()
            return float(limits.current.monthly_usage_usd)
        except Exception as exc:
            log.warning("could not read Apify usage: %s", exc)
            return None
