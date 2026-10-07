"""Bundles the collaborators every job needs, so jobs stay plain functions and tests can inject fakes."""
from __future__ import annotations

from dataclasses import dataclass

from .apify_gw import ApifyGateway, RealApify
from .config import Config
from .fetch import Fetcher
from .llm import LLMClient, OpenRouter
from .notify import Notifier
from .state import State


@dataclass
class Context:
    cfg: Config
    state: State
    llm: LLMClient
    apify: ApifyGateway
    fetcher: Fetcher
    notifier: Notifier

    @classmethod
    def create(cls, cfg: Config) -> "Context":
        state = State(cfg.db_path)
        return cls(
            cfg=cfg,
            state=state,
            llm=OpenRouter(cfg.openrouter_key, state, free_models=cfg.free_models, paid_model=cfg.paid_model,
                           max_paid_price_per_mtok=cfg.max_paid_price_per_mtok,
                           monthly_budget_usd=cfg.llm_monthly_budget_usd),
            apify=RealApify(cfg.apify_token),
            fetcher=Fetcher(),
            notifier=Notifier(cfg),
        )
