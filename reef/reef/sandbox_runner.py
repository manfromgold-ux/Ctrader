"""Child-process entry point for sandbox.run(). Reads one JSON request on stdin, writes one JSON reply line.

Usage: python -I sandbox_runner.py <dir containing extractor.py>
"""
import io
import json
import sys
import traceback
from contextlib import redirect_stdout


def main() -> None:
    sys.path.insert(0, sys.argv[1])
    request = json.loads(sys.stdin.read())
    captured = io.StringIO()
    reply: dict
    try:
        with redirect_stdout(captured):
            import extractor  # noqa: PLC0415 - imported from the sandbox dir on purpose

            if request["op"] == "start_urls":
                urls = extractor.start_urls(dict(request.get("params") or {}))
                if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                    raise TypeError("start_urls() must return a list of strings")
                data = urls
            elif request["op"] == "parse":
                data = []
                for page in request["pages"]:
                    result = extractor.parse(page["html"], page["url"])
                    if not isinstance(result, dict):
                        raise TypeError("parse() must return a dict with 'items' and 'next'")
                    items = result.get("items") or []
                    nxt = result.get("next") or []
                    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                        raise TypeError("parse()['items'] must be a list of dicts")
                    if not isinstance(nxt, list) or not all(isinstance(u, str) for u in nxt):
                        raise TypeError("parse()['next'] must be a list of URL strings")
                    data.append({"url": page["url"], "items": items, "next": nxt})
            else:
                raise ValueError(f"unknown op {request['op']!r}")
        json.dumps(data)  # must be serialisable
        reply = {"ok": True, "data": data}
    except Exception as exc:  # report every failure back to the parent as data
        tb = traceback.format_exc().splitlines()
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": tb[-8:]}
    logs = captured.getvalue().splitlines()[-20:]
    reply.setdefault("logs", [])
    reply["logs"] = logs + reply["logs"]
    sys.stdout.write(json.dumps(reply, default=str) + "\n")


if __name__ == "__main__":
    main()
