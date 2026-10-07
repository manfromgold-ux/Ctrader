import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeApify, FakeLLM, FakeSite  # noqa: E402

from reef.config import Config  # noqa: E402
from reef.context import Context  # noqa: E402
from reef.fetch import Fetcher  # noqa: E402
from reef.notify import Notifier  # noqa: E402
from reef.state import State  # noqa: E402


@pytest.fixture
def site():
    s = FakeSite()
    yield s
    s.stop()


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, apify_token="t", openrouter_key="k",
                  blocked_domains=["linkedin.com"])


@pytest.fixture
def make_ctx(cfg):
    def _make(llm: FakeLLM, apify: FakeApify | None = None) -> Context:
        return Context(cfg=cfg, state=State(cfg.db_path), llm=llm, apify=apify or FakeApify(),
                       fetcher=Fetcher(min_delay=0), notifier=Notifier(cfg))
    return _make
