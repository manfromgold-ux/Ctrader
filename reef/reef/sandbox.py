"""Static checks and an isolated subprocess for running LLM-written extractor code.

Extractor code is written by a model that has just read untrusted HTML, so it is treated as untrusted:
  1. An AST allowlist rejects imports and builtins that could touch the network, files or the interpreter.
  2. The code runs in a separate Python process with an empty environment (no API keys), CPU/memory
     limits and a wall-clock timeout. It only ever receives HTML that Reef fetched itself.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_IMPORTS = {
    "re", "json", "html", "math", "datetime", "typing", "decimal", "string", "itertools",
    "collections", "dataclasses", "functools", "urllib", "urllib.parse", "unicodedata", "parsel",
}
FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "memoryview", "help", "exit", "quit", "__builtins__", "__loader__",
    "__spec__",
}
REQUIRED_FUNCTIONS = ("start_urls", "parse")
RUNNER = Path(__file__).with_name("sandbox_runner.py")


class UnsafeCode(ValueError):
    pass


def check_code(code: str) -> None:
    """Raise UnsafeCode (with a human-readable reason) if the extractor breaks the rules."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise UnsafeCode(f"SyntaxError: {exc.msg} (line {exc.lineno})") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    raise UnsafeCode(f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_IMPORTS:
                raise UnsafeCode(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise UnsafeCode(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeCode(f"dunder attribute access '.{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise UnsafeCode("global/nonlocal statements are not allowed")

    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = [f for f in REQUIRED_FUNCTIONS if f not in defined]
    if missing:
        raise UnsafeCode(f"missing required top-level function(s): {', '.join(missing)}")


@dataclass
class SandboxResult:
    ok: bool
    data: Any = None
    error: str = ""
    logs: list[str] = field(default_factory=list)


def _limits() -> None:  # pragma: no cover - runs in the child process
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def run(code: str, op: str, payload: dict, timeout: float = 60.0) -> SandboxResult:
    """Run `op` ('start_urls' or 'parse') of the extractor in an isolated child process."""
    try:
        check_code(code)
    except UnsafeCode as exc:
        return SandboxResult(ok=False, error=f"rejected by safety check: {exc}")

    with tempfile.TemporaryDirectory(prefix="reef-sbx-") as tmp:
        Path(tmp, "extractor.py").write_text(code, encoding="utf-8")
        request = json.dumps({"op": op, **payload})
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(RUNNER), tmp],
                input=request,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0"},
                cwd=tmp,
                preexec_fn=_limits if sys.platform != "win32" else None,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(ok=False, error=f"timed out after {timeout:.0f}s")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        tail = proc.stderr.strip().splitlines()[-5:]
        return SandboxResult(ok=False, error="no output from extractor process: " + " | ".join(tail))
    try:
        reply = json.loads(lines[-1])
    except json.JSONDecodeError:
        return SandboxResult(ok=False, error="extractor process returned non-JSON output")
    return SandboxResult(ok=bool(reply.get("ok")), data=reply.get("data"), error=reply.get("error", ""),
                         logs=reply.get("logs", []))
