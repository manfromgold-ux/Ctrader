"""Prompts for every LLM task. Outputs use fenced blocks, which free models produce more reliably than
code escaped inside JSON strings."""
from __future__ import annotations

import json

from .spec import ALLOWED_CATEGORIES, PARAM_TYPES

EXTRACTOR_RULES = """Extractor module rules (Python 3.12):
- Allowed imports ONLY: re, json, html, math, datetime, typing, decimal, string, itertools, collections,
  dataclasses, functools, urllib.parse, unicodedata, and `from parsel import Selector`.
- No network, files, eval/exec, getattr, globals, or dunder attributes. Never print secrets.
- Define exactly these top-level functions:
    def start_urls(params: dict) -> list[str]
        Build the first page URL(s) from user input params (use defaults when a param is missing).
    def parse(html: str, url: str) -> dict
        Return {"items": [ {field: value, ...}, ... ], "next": [absolute URLs of further listing pages]}.
- Prefer stable hooks: JSON-LD (<script type="application/ld+json">), microdata (itemprop), ids, semantic
  tags, data-* attributes, readable class names. Avoid hashed class names like "css-1q2w3e" or "sc-AxjAm".
- Use urllib.parse.urljoin(url, href) so every URL is absolute.
- Strip whitespace. Numbers must be int/float (parse "1 234,50 €" correctly for the locale). Dates as ISO
  8601 strings. Missing values are None, never "N/A".
- "next" contains only pagination/listing URLs on the same site, never detail pages already in items.
- Must not crash on unexpected markup: guard every lookup.
- Never extract personal data about private individuals (no emails, phone numbers, or names of people)."""

SCOUT_SYSTEM = """You find profitable gaps for a marketplace of paid web scrapers (Apify Store). Buyers are
businesses that need fresh structured data from one website on a recurring basis. You only propose sites
whose public pages can be read without logging in."""


def scout_user(n: int, exclude: list[str], blocked: list[str]) -> str:
    return f"""Propose {n} websites to build a scraper for. Requirements:
- Public listing pages with structured records (e.g. public tenders and procurement notices, B2B company
  directories, job boards, real-estate listings, auctions, product catalogs of mid-size retailers,
  event listings, government registries, app or software review sites, price lists).
- Server-rendered HTML preferred (data visible in page source), no login, no paywall.
- Long tail beats famous: mid-size sites, non-English countries and niche industries are less served by
  existing scrapers. Mix countries and languages.
- Records must be about businesses, products, listings or notices - NOT private individuals.
- Someone would plausibly pay $2 per 1,000 records for it every week. Say who.
- Do NOT propose any of these (already covered or forbidden): {", ".join(sorted(set(exclude + blocked))[:300])}

Respond with one ```json block holding a list of objects:
[{{"domain": "example.pl", "start_url": "https://example.pl/listings", "data": "what each record is",
   "buyers": "who pays and why", "search_terms": ["2-3 words to search existing scrapers"]}}]"""


BUILD_SYSTEM = f"""You build production web-data extractors that are sold as paid Apify Actors. You get a
target site and the (trimmed) HTML of its listing page. Output exactly two fenced blocks: first ```json
with the Actor spec, then ```python with the extractor module.

Spec JSON keys:
- "title": Store title, max 60 chars, format "<Site name> Scraper" or "<Site> <Data> Scraper".
- "description": 1-2 sentences (80-300 chars) saying what data you get and why it is useful.
- "seo_title" (max 60 chars) and "seo_description" (max 160 chars).
- "categories": 1-3 of {sorted(ALLOWED_CATEGORIES)}.
- "params": user inputs used by start_urls, each {{"name", "title", "type", "description", "default",
  "options", "required"}}; type is one of {sorted(PARAM_TYPES)} ("options" only for enum). Typical: a search
  keyword, a category or region, a sort order. Keep it to 1-4 params. Names: camelCase identifiers.
- "fields": output fields, each {{"name", "type", "description", "required"}}; type is one of string,
  number, integer, boolean, array, object. Include "url" (absolute record URL). 5-15 fields. Mark a field
  required only if every record on the page has it.
- "test_params": params for a quick test run that returns results.
- "use_cases": 3-5 short concrete use cases.
- "min_items": how many items one listing page reliably returns (usually 5-20).

{EXTRACTOR_RULES}"""


def build_user(candidate: dict, page_url: str, trimmed_html: str, feedback: str = "") -> str:
    text = f"""Target site: {candidate['domain']}
What to extract: {candidate.get('summary', '')}
Who buys it: {candidate.get('buyers', '')}
Listing page URL: {page_url}

Trimmed HTML of that page:
```html
{trimmed_html}
```"""
    if feedback:
        text += f"""

Your previous attempt failed. Fix these problems and return BOTH blocks again in full:
{feedback}"""
    return text


HEAL_SYSTEM = f"""You maintain a paid web scraper whose target site changed. Users depend on the exact
output fields, so you must keep every field name and type exactly as specified and only change how the
values are found. Output exactly one ```python block with the complete corrected extractor module.

{EXTRACTOR_RULES}"""


def heal_user(spec: dict, code: str, failure: str, page_url: str, trimmed_html: str) -> str:
    fields = json.dumps([{k: f[k] for k in ("name", "type", "required", "description")} for f in spec["fields"]],
                        indent=1)
    params = json.dumps(spec.get("test_params", {}))
    return f"""Output field contract (must not change):
{fields}

Test input params: {params}

Current extractor code:
```python
{code}
```

What is failing now:
{failure}

Current HTML of {page_url} (trimmed):
```html
{trimmed_html}
```"""


CLONE_SYSTEM = SCOUT_SYSTEM


def clone_user(spec: dict, users30: int, n: int, exclude: list[str], blocked: list[str]) -> str:
    return f"""This scraper is selling well ({users30} active users in 30 days):
Title: {spec['title']}
Site: {spec['domain']}
Description: {spec['description']}
Fields: {", ".join(f["name"] for f in spec["fields"])}

Propose {n} sibling websites that publish the same kind of records for other countries, regions or
competitors, so the same buyers (or their peers elsewhere) would pay for them too.
Do NOT propose: {", ".join(sorted(set(exclude + blocked))[:300])}

Respond with one ```json block holding a list of objects:
[{{"domain": "...", "start_url": "...", "data": "...", "buyers": "...", "search_terms": ["..."]}}]"""
