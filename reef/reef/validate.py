"""Checks extracted items against the spec's field contract. Used at build time, after every heal, and
on every canary run - a patch that changes the shape of the data never ships."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .spec import ActorSpec

TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}
REQUIRED_FILL_RATE = 0.9
TYPE_OK_RATE = 0.95


@dataclass
class Report:
    ok: bool
    count: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.count} items"]
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        return " | ".join(parts)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0) or (
        isinstance(value, str) and not value.strip())


def check_items(spec: ActorSpec, items: list[dict]) -> Report:
    errors: list[str] = []
    warnings: list[str] = []
    n = len(items)
    if n < spec.min_items:
        errors.append(f"expected at least {spec.min_items} items, got {n}")
    if n == 0:
        return Report(ok=False, count=0, errors=errors)

    declared = set(spec.field_names)
    extra = sorted({k for it in items for k in it} - declared)
    if extra:
        warnings.append(f"undeclared keys will be dropped: {extra[:8]}")

    for f in spec.fields:
        present = [it.get(f.name) for it in items if not _empty(it.get(f.name))]
        fill = len(present) / n
        if f.required and fill < REQUIRED_FILL_RATE:
            errors.append(f"required field '{f.name}' filled in only {fill:.0%} of items")
        elif not f.required and fill == 0:
            warnings.append(f"optional field '{f.name}' is always empty")
        if present:
            good = sum(1 for v in present if TYPE_CHECKS[f.type](v))
            if good / len(present) < TYPE_OK_RATE:
                bad = next(v for v in present if not TYPE_CHECKS[f.type](v))
                errors.append(f"field '{f.name}' should be {f.type}, got e.g. {type(bad).__name__} {str(bad)[:40]!r}")

    if "url" in declared:
        urls = [it.get("url") for it in items if isinstance(it.get("url"), str)]
        if urls and sum(1 for u in urls if u.startswith(("http://", "https://"))) / len(urls) < REQUIRED_FILL_RATE:
            errors.append("field 'url' must hold absolute http(s) URLs")
        if len(set(urls)) < max(1, len(urls) // 2):
            warnings.append("many duplicate 'url' values - records may not be distinct")

    return Report(ok=not errors, count=n, errors=errors, warnings=warnings)
