import copy

import pytest
from fakes import SPEC

from reef.spec import ActorSpec, SpecError, normalize, slugify
from reef.validate import check_items


def test_normalize_valid_spec():
    spec = normalize(copy.deepcopy(SPEC), "www.Tenders.example.pl")
    assert spec.domain == "tenders.example.pl"
    assert spec.name == "tenders-example-pl-scraper"
    assert spec.categories == ["LEAD_GENERATION", "BUSINESS"]  # unknown category dropped
    assert spec.field_names == ["url", "title", "buyer", "value_eur", "deadline"]
    assert ActorSpec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s["fields"].append({"name": "contact_email", "type": "string"}), "personal data"),
    (lambda s: s["fields"].append({"name": "owner_phone", "type": "string"}), "personal data"),
    (lambda s: s.update(fields=[f for f in s["fields"] if f["name"] != "url"]), "'url' field"),
    (lambda s: s.update(test_params={"nope": 1}), "undefined params"),
    (lambda s: s["params"].append({"name": "maxItems", "type": "integer"}), "reserved"),
    (lambda s: s.update(description="short"), "description"),
])
def test_normalize_rejects(mutate, message):
    raw = copy.deepcopy(SPEC)
    mutate(raw)
    with pytest.raises(SpecError, match=message):
        normalize(raw, "example.pl")


def test_slugify():
    assert slugify("Hello, World! Scraper") == "hello-world-scraper"
    assert len(slugify("x" * 200)) <= 55


def _items(n=5, **override):
    base = {"url": "https://e.pl/1", "title": "T", "buyer": "B", "value_eur": 10.0, "deadline": "2026-01-01"}
    return [{**base, "url": f"https://e.pl/{i}", **override} for i in range(n)]


def test_check_items():
    spec = normalize(copy.deepcopy(SPEC), "e.pl")
    assert check_items(spec, _items()).ok
    assert not check_items(spec, _items(2)).ok  # below min_items
    bad = check_items(spec, _items(title=""))
    assert not bad.ok and "title" in bad.errors[0]
    typed = check_items(spec, _items(value_eur="10 EUR"))
    assert not typed.ok and "should be number" in typed.errors[0]
    rel = check_items(spec, [{**i, "url": "/relative"} for i in _items()])
    assert not rel.ok and "absolute" in " ".join(rel.errors)
    optional_missing = check_items(spec, _items(value_eur=None))
    assert optional_missing.ok and optional_missing.warnings
