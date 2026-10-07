"""The Actor spec: what an Actor is called, what input it takes, and the output fields it promises.

The field list is a contract with paying users. Healing may rewrite the extractor code, but never the fields.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

ALLOWED_CATEGORIES = {
    "AUTOMATION", "BUSINESS", "DEVELOPER_TOOLS", "ECOMMERCE", "JOBS", "LEAD_GENERATION", "MARKETING",
    "NEWS", "REAL_ESTATE", "TRAVEL", "OTHER",
}
FIELD_TYPES = {"string", "number", "integer", "boolean", "array", "object"}
PARAM_TYPES = {"string", "integer", "boolean", "enum", "stringList"}
RESERVED_INPUTS = {"maxItems", "maxPages", "proxyConfiguration", "debugSaveHtml"}
# Output fields that would carry personal data about private individuals. Reef refuses to build these.
PERSONAL_FIELD = re.compile(
    r"(e[-_]?mail|phone|mobile|telephone|whatsapp|birth|ssn|passport|national_?id|home_?address|"
    r"first_?name|last_?name|surname|full_?name|person_?name|owner_?name|ip_?address)",
    re.IGNORECASE,
)
IDENT = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,40}$")


class SpecError(ValueError):
    pass


@dataclass
class ParamSpec:
    name: str
    title: str
    type: str
    description: str
    default: Any = None
    options: list[str] = field(default_factory=list)
    required: bool = False


@dataclass
class FieldSpec:
    name: str
    type: str
    description: str
    required: bool = True


@dataclass
class ActorSpec:
    name: str
    title: str
    description: str
    domain: str
    seo_title: str
    seo_description: str
    categories: list[str]
    params: list[ParamSpec]
    fields: list[FieldSpec]
    test_params: dict[str, Any]
    use_cases: list[str]
    min_items: int = 3

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ActorSpec":
        return cls(
            **{k: v for k, v in d.items() if k not in ("params", "fields")},
            params=[ParamSpec(**p) for p in d.get("params", [])],
            fields=[FieldSpec(**f) for f in d.get("fields", [])],
        )

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


def slugify(text: str, max_len: int = 55) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "scraper"


def _actor_name(host: str) -> str:
    """Apify Actor names: lowercase letters, digits and dashes; start with a letter to be safe."""
    name = slugify(f"{host.replace('.', '-')}-scraper")
    return name if name[0].isalpha() else f"site-{name}"[:55]


def _clip(text: Any, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def normalize(raw: dict, domain: str) -> ActorSpec:
    """Turn a model-produced spec into a validated ActorSpec, or raise SpecError explaining what to fix."""
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    errors: list[str] = []

    title = _clip(raw.get("title"), 60)
    if len(title) < 8:
        errors.append("title is missing or too short")
    description = _clip(raw.get("description"), 300)
    if len(description) < 40:
        errors.append("description must be at least 40 characters")

    params: list[ParamSpec] = []
    for p in raw.get("params") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", ""))
        ptype = str(p.get("type", "string"))
        if not IDENT.match(name) or name in RESERVED_INPUTS:
            errors.append(f"param name '{name}' is invalid or reserved")
            continue
        if ptype not in PARAM_TYPES:
            errors.append(f"param '{name}' has unsupported type '{ptype}' (use one of {sorted(PARAM_TYPES)})")
            continue
        options = [str(o) for o in p.get("options") or []]
        if ptype == "enum" and not options:
            errors.append(f"enum param '{name}' needs a non-empty 'options' list")
        params.append(ParamSpec(
            name=name, title=_clip(p.get("title") or name, 60), type=ptype,
            description=_clip(p.get("description") or name, 300), default=p.get("default"),
            options=options, required=bool(p.get("required", False)),
        ))

    fields: list[FieldSpec] = []
    seen: set[str] = set()
    for f in raw.get("fields") or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", ""))
        ftype = str(f.get("type", "string"))
        if not IDENT.match(name) or name in seen:
            errors.append(f"field name '{name}' is invalid or duplicated")
            continue
        if PERSONAL_FIELD.search(name):
            errors.append(f"field '{name}' looks like personal data; remove it")
            continue
        if ftype not in FIELD_TYPES:
            errors.append(f"field '{name}' has unsupported type '{ftype}'")
            continue
        seen.add(name)
        fields.append(FieldSpec(name=name, type=ftype, description=_clip(f.get("description") or name, 200),
                                required=bool(f.get("required", True))))
    if len(fields) < 3:
        errors.append("define at least 3 output fields")
    if not any(f.required for f in fields):
        errors.append("mark at least one field as required")
    if "url" not in seen:
        errors.append("include a 'url' field with the absolute URL of each record's detail page or source page")

    test_params = raw.get("test_params") or {}
    if not isinstance(test_params, dict):
        errors.append("test_params must be an object")
        test_params = {}
    unknown = set(test_params) - {p.name for p in params}
    if unknown:
        errors.append(f"test_params uses undefined params: {sorted(unknown)}")

    categories = [c for c in (raw.get("categories") or []) if c in ALLOWED_CATEGORIES][:3] or ["OTHER"]
    use_cases = [_clip(u, 160) for u in (raw.get("use_cases") or []) if str(u).strip()][:6]
    min_items = raw.get("min_items", 3)
    min_items = max(1, min(int(min_items), 20)) if isinstance(min_items, (int, float)) else 3

    if errors:
        raise SpecError("; ".join(errors))

    host = domain.lower().removeprefix("www.")
    return ActorSpec(
        name=_actor_name(host),
        title=title,
        description=description,
        domain=host,
        seo_title=_clip(raw.get("seo_title") or title, 60),
        seo_description=_clip(raw.get("seo_description") or description, 160),
        categories=categories,
        params=params,
        fields=fields,
        test_params=test_params,
        use_cases=use_cases,
        min_items=min_items,
    )
