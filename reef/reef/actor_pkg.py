"""Assembles a complete Apify Actor source tree from a spec, an extractor module and sample output."""
from __future__ import annotations

import json
from pathlib import Path

from .spec import ActorSpec

TEMPLATE_DIR = Path(__file__).with_name("actor_template")
TEMPLATE_FILES = ["Dockerfile", "requirements.txt", "src/__init__.py", "src/__main__.py", "src/main.py"]
PARAM_EDITORS = {"string": "textfield", "integer": "number", "boolean": "checkbox", "stringList": "stringList"}


def input_schema(spec: ActorSpec) -> dict:
    props: dict[str, dict] = {}
    for p in spec.params:
        prop: dict = {"title": p.title, "description": p.description}
        if p.type == "enum":
            prop.update(type="string", editor="select", enum=p.options)
        elif p.type == "stringList":
            prop.update(type="array", editor="stringList")
        else:
            prop.update(type=p.type, editor=PARAM_EDITORS[p.type])
        example = spec.test_params.get(p.name, p.default)
        if example is not None:
            prop["prefill" if p.type in ("string", "stringList") else "default"] = example
        props[p.name] = prop
    props["maxItems"] = {
        "title": "Max results", "type": "integer", "editor": "number", "minimum": 1, "default": 100,
        "description": "Stop after this many results. You are charged per result, so this also caps the cost.",
    }
    props["maxPages"] = {
        "title": "Max pages", "type": "integer", "editor": "number", "minimum": 1, "default": 20,
        "description": "Maximum number of pages to load (listing pages and pagination).",
    }
    props["proxyConfiguration"] = {
        "title": "Proxy configuration", "type": "object", "editor": "proxy", "sectionCaption": "Advanced",
        "description": "Proxies help avoid blocking. Datacenter proxies are enough for most runs.",
        "prefill": {"useApifyProxy": True}, "default": {"useApifyProxy": True},
    }
    props["debugSaveHtml"] = {
        "title": "Save raw HTML (debug)", "type": "boolean", "editor": "checkbox", "default": False,
        "description": "Store the HTML of the first pages in the key-value store, for troubleshooting.",
    }
    return {
        "title": f"{spec.title} input",
        "type": "object",
        "schemaVersion": 1,
        "properties": props,
        "required": [p.name for p in spec.params if p.required],
    }


def dataset_schema(spec: ActorSpec) -> dict:
    json_types = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean",
                  "array": "array", "object": "object"}
    display_formats = {"string": "text", "number": "number", "integer": "number", "boolean": "boolean",
                       "array": "array", "object": "object"}
    overview = spec.field_names[:8]
    return {
        "actorSpecification": 1,
        "fields": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                f.name: {"type": [json_types[f.type], "null"], "description": f.description} for f in spec.fields
            },
        },
        "views": {
            "overview": {
                "title": "Overview",
                "transformation": {"fields": overview},
                "display": {
                    "component": "table",
                    "properties": {
                        name: {"label": name.replace("_", " ").title(),
                               "format": "link" if name == "url" else display_formats[spec.fields[i].type]}
                        for i, name in enumerate(overview)
                    },
                },
            }
        },
    }


def actor_json(spec: ActorSpec, version: str) -> dict:
    return {
        "actorSpecification": 1,
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "version": version,
        "buildTag": "latest",
        "input": "./input_schema.json",
        "storages": {"dataset": "./dataset_schema.json"},
        "dockerfile": "../Dockerfile",
    }


def readme(spec: ActorSpec, sample_items: list[dict], price_per_1000: float) -> str:
    sample = json.dumps(sample_items[:2], indent=2, ensure_ascii=False, default=str)
    if len(sample) > 3000:
        sample = json.dumps(sample_items[:1], indent=2, ensure_ascii=False, default=str)[:3000]
    lines = [
        f"# {spec.title}",
        "",
        spec.description,
        "",
        f"## What data does {spec.title} extract?",
        "",
        "| Field | Description |",
        "|---|---|",
        *[f"| `{f.name}` | {f.description} |" for f in spec.fields],
        "",
    ]
    if spec.use_cases:
        lines += ["## Use cases", "", *[f"- {u}" for u in spec.use_cases], ""]
    lines += [
        "## How to use it",
        "",
        "1. Fill in the input fields (or keep the prefilled example).",
        "2. Set **Max results** to cap the run size and cost.",
        "3. Click **Start** and download results as JSON, CSV, Excel or via the API.",
        "",
        "### Input",
        "",
        "| Input | Description |",
        "|---|---|",
        *[f"| `{p.name}` | {p.description} |" for p in spec.params],
        "| `maxItems` | Maximum number of results. |",
        "| `maxPages` | Maximum number of pages to load. |",
        "",
        "## Output example",
        "",
        "```json",
        sample,
        "```",
        "",
        "## Pricing",
        "",
        f"Pay per result: **${price_per_1000:.2f} per 1,000 results**. Platform usage is included. "
        "Runs stop at **Max results**, so cost is predictable.",
        "",
        "## Is it legal to scrape this site?",
        "",
        f"This Actor extracts only publicly available, non-personal information from {spec.domain}. "
        "It does not log in or bypass access controls. Make sure your use of the data complies with the "
        "site's terms and the laws that apply to you.",
        "",
        "## Reliability",
        "",
        "This Actor is checked automatically several times a day. When the website changes its layout, "
        "the extractor is repaired and re-tested against the same output fields, so your integrations "
        "keep working.",
        "",
    ]
    return "\n".join(lines)


def source_files(spec: ActorSpec, extractor_code: str, sample_items: list[dict], version: str,
                 price_per_1000: float) -> list[dict]:
    """Return the Actor source as a list of {name, format, content} dicts for the Apify API."""
    files = {name: (TEMPLATE_DIR / name).read_text(encoding="utf-8") for name in TEMPLATE_FILES}
    files["src/extractor.py"] = extractor_code
    files["src/reef_meta.json"] = json.dumps(
        {"fields": [{"name": f.name, "type": f.type} for f in spec.fields], "domain": spec.domain}, indent=2)
    files[".actor/actor.json"] = json.dumps(actor_json(spec, version), indent=2)
    files[".actor/input_schema.json"] = json.dumps(input_schema(spec), indent=2, ensure_ascii=False)
    files[".actor/dataset_schema.json"] = json.dumps(dataset_schema(spec), indent=2)
    files["README.md"] = readme(spec, sample_items, price_per_1000)
    return [{"name": name, "format": "TEXT", "content": content} for name, content in files.items()]


def write_tree(files: list[dict], root: Path) -> Path:
    for f in files:
        path = root / f["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8")
    return root
