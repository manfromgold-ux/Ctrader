import copy
import json

from fakes import EXTRACTOR_V1, SPEC, FakeApify

from reef import actor_pkg
from reef.spec import normalize


def _files(base="http://127.0.0.1:1"):
    spec = normalize(copy.deepcopy(SPEC), "127.0.0.1")
    sample = [{"url": "https://x/1", "title": "Road", "buyer": "City", "value_eur": 1.0, "deadline": "2026-01-01"}]
    return spec, {f["name"]: f["content"] for f in
                  actor_pkg.source_files(spec, EXTRACTOR_V1.format(base=base), sample, "0.0", 2.0)}


def test_package_layout_and_schemas():
    spec, files = _files()
    assert {"Dockerfile", "requirements.txt", "src/main.py", "src/__main__.py", "src/extractor.py",
            "src/reef_meta.json", ".actor/actor.json", ".actor/input_schema.json", ".actor/dataset_schema.json",
            "README.md"} <= set(files)
    actor = json.loads(files[".actor/actor.json"])
    assert actor["name"] == spec.name and actor["version"] == "0.0"
    inp = json.loads(files[".actor/input_schema.json"])
    assert inp["properties"]["query"]["prefill"] == "roads"
    assert inp["properties"]["proxyConfiguration"]["editor"] == "proxy"
    ds = json.loads(files[".actor/dataset_schema.json"])
    assert ds["fields"]["properties"]["value_eur"]["type"] == ["number", "null"]
    assert ds["views"]["overview"]["display"]["properties"]["url"]["format"] == "link"
    assert "$2.00 per 1,000 results" in files["README.md"]


def test_engine_runs_locally_against_site(site):
    """The real engine + Apify SDK, run locally: pagination, de-dup, field contract, limits."""
    spec, _ = _files(site.base)
    apify = FakeApify()
    files = actor_pkg.source_files(spec, EXTRACTOR_V1.format(base=site.base), [], "0.0", 2.0)
    actor_id = apify.deploy(spec, files)
    assert apify.build(actor_id)[0]

    result = apify.run(actor_id, {"query": "roads", "maxItems": 100, "maxPages": 5}, max_items=100)
    assert result.succeeded, result.status_message
    assert len(result.items) == 12  # both pages followed
    assert set(result.items[0]) == set(spec.field_names)
    assert result.items[0]["value_eur"] == 102500.0

    limited = apify.run(actor_id, {"query": "roads", "maxItems": 4, "debugSaveHtml": True}, max_items=100)
    assert len(limited.items) == 4
    assert limited.debug_pages and "article" in limited.debug_pages[0][1]

    site.layout = "v3"
    empty = apify.run(actor_id, {"query": "roads"}, max_items=10)
    assert empty.succeeded and empty.items == []  # nothing found is not a crash
