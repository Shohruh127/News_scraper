"""M0.1 - Ollama capability probe. Throwaway code, see spikes/README.md.

Usage:
    python probe_ollama.py tags
    python probe_ollama.py fetch
    python probe_ollama.py bench --model gemma4:latest --n 20
    python probe_ollama.py concurrency --model gemma4:latest
    python probe_ollama.py longprompt --model gemma4:31b
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
import trafilatura

# Windows consoles default to cp1252 and crash on characters like U+2011.
# Do this before anything prints.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)


def base_url() -> str:
    for line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
        if line.startswith("OLLAMA_BASE_URL="):
            return line.split("=", 1)[1].strip().rstrip("/")
    sys.exit("OLLAMA_BASE_URL not found in .env")


# The classification schema from IMPLEMENTATION_PLAN.md T0.2 step 5.
TOPICS = [
    "frontier_models", "ai_agents", "new_approaches", "speech_voice", "robotics",
    "fintech", "govtech", "production_engineering", "startups", "technical_talks",
    "safety_security", "irrelevant",
]
MATURITIES = [
    "production_deployment", "live_product", "reproducible_open_source",
    "public_pilot", "announcement_only", "paper_only",
]

SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "primary_topic": {"type": "string", "enum": TOPICS},
        "maturity": {"type": "string", "enum": MATURITIES},
        "novelty": {"type": "integer", "minimum": 1, "maximum": 10},
        "evidence": {"type": "integer", "minimum": 1, "maximum": 10},
        "production_readiness": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "primary_topic", "maturity", "novelty", "evidence",
                 "production_readiness", "reason"],
}

PROMPT = """You are a technical editor for an AI-engineering news digest.
Read the article and classify it. Return JSON only.

Scoring guidance:
- novelty: 1 = rehash of known news, 10 = genuinely new capability or result
- evidence: 1 = vendor claim only, 10 = reproducible artifacts (weights, repo, independent eval)
- production_readiness: 1 = paper or announcement, 10 = deployed and documented

ARTICLE
Title: {title}
Source: {source}
---
{text}
"""


# ---------------------------------------------------------------- tags

def cmd_tags(args):
    r = httpx.get(f"{base_url()}/api/tags", timeout=30)
    r.raise_for_status()
    data = r.json()
    (OUT / "ollama_raw.json").write_text(json.dumps(data, indent=2))

    print(f"{'TAG':<22} {'PARAMS':>8} {'QUANT':>10} {'SIZE':>9}  DIGEST")
    print("-" * 78)
    for m in data["models"]:
        d = m.get("details", {})
        print(f"{m['name']:<22} {d.get('parameter_size', '?'):>8} "
              f"{d.get('quantization_level', '?'):>10} "
              f"{m['size'] / 1e9:>8.2f}G  {m['digest'][:16]}")
    print(f"\n{len(data['models'])} model(s). Raw -> {OUT / 'ollama_raw.json'}")


# ---------------------------------------------------------------- fetch

FEEDS = [
    ("openai", "rss", "https://openai.com/news/rss.xml"),
    ("hf_papers", "hf", "https://huggingface.co/api/daily_papers?limit=6"),
    ("github", "gh", "https://api.github.com/repos/ollama/ollama/releases?per_page=3"),
]


def _extract(url: str) -> str:
    try:
        html = trafilatura.fetch_url(url)
        if not html:
            return ""
        return trafilatura.extract(html, include_comments=False) or ""
    except Exception as e:  # noqa: BLE001
        print(f"    extract failed {url}: {e}")
        return ""


def cmd_fetch(args):
    import feedparser

    articles = []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "news-radar-spike/0.1"}) as c:
        for name, kind, url in FEEDS:
            print(f"[{name}] {url}")
            try:
                if kind == "rss":
                    feed = feedparser.parse(c.get(url).text)
                    for e in feed.entries[:4]:
                        text = _extract(e.link)
                        if len(text) > 400:
                            articles.append({"source": name, "title": e.title,
                                             "url": e.link, "text": text[:12000]})
                            print(f"    OK {len(text):>6} chars  {e.title[:60]}")
                elif kind == "hf":
                    for p in c.get(url).json()[:4]:
                        pp = p.get("paper", p)
                        text = f"{pp.get('title', '')}\n\n{pp.get('summary', '')}"
                        if len(text) > 400:
                            articles.append({
                                "source": name, "title": pp.get("title", ""),
                                "url": f"https://huggingface.co/papers/{pp.get('id', '')}",
                                "text": text[:12000]})
                            print(f"    OK {len(text):>6} chars  {pp.get('title', '')[:60]}")
                elif kind == "gh":
                    for rel in c.get(url).json()[:3]:
                        text = f"{rel['name']}\n\n{rel.get('body') or ''}"
                        if len(text) > 400:
                            articles.append({"source": name, "title": rel["name"],
                                             "url": rel["html_url"], "text": text[:12000]})
                            print(f"    OK {len(text):>6} chars  {rel['name'][:60]}")
            except Exception as e:  # noqa: BLE001
                print(f"    FAILED: {e}")

    (OUT / "sample_articles.json").write_text(
        json.dumps(articles, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(articles)} articles -> {OUT / 'sample_articles.json'}")
    if len(articles) < 10:
        print("WARNING: fewer than 10 articles. Benchmark will cycle through them.")


# ---------------------------------------------------------------- bench

def _validate(obj) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "not an object"
    for k in SCHEMA["required"]:
        if k not in obj:
            return False, f"missing {k}"
    if obj["primary_topic"] not in TOPICS:
        return False, f"bad topic {obj['primary_topic']!r}"
    if obj["maturity"] not in MATURITIES:
        return False, f"bad maturity {obj['maturity']!r}"
    for k in ("novelty", "evidence", "production_readiness"):
        v = obj[k]
        if not isinstance(v, int) or not 1 <= v <= 10:
            return False, f"bad {k}={v!r}"
    if not isinstance(obj["relevant"], bool):
        return False, "relevant not bool"
    return True, ""


def cmd_bench(args):
    arts = json.loads((OUT / "sample_articles.json").read_text(encoding="utf-8"))
    if not arts:
        sys.exit("No sample articles. Run: probe_ollama.py fetch")

    url = f"{base_url()}/api/chat"
    rows, lat = [], []
    print(f"model={args.model}  n={args.n}  timeout={args.timeout}s\n")

    with httpx.Client(timeout=args.timeout) as c:
        for i in range(args.n):
            a = arts[i % len(arts)]
            payload = {
                "model": args.model,
                "messages": [{"role": "user", "content": PROMPT.format(
                    title=a["title"], source=a["source"], text=a["text"][:8000])}],
                "stream": False,
                "format": SCHEMA,
                "options": {"temperature": 0},
            }
            t0 = time.perf_counter()
            try:
                r = c.post(url, json=payload)
                dt = time.perf_counter() - t0
                r.raise_for_status()
                body = r.json()
                content = body["message"]["content"]
                try:
                    parsed = json.loads(content)
                    ok, err = _validate(parsed)
                except json.JSONDecodeError as e:
                    parsed, ok, err = None, False, f"unparseable: {e}"
                lat.append(dt)
                rows.append({"i": i, "article": a["title"][:70], "seconds": round(dt, 2),
                             "parseable": parsed is not None, "schema_ok": ok,
                             "error": err, "result": parsed,
                             "eval_count": body.get("eval_count"),
                             "total_duration_ns": body.get("total_duration")})
                flag = "OK " if ok else "BAD"
                extra = "" if ok else f"  <- {err}"
                topic = parsed.get("primary_topic", "?") if parsed else "?"
                print(f"  {i + 1:>3}/{args.n}  {dt:>7.2f}s  {flag}  {topic:<22}{extra}")
            except Exception as e:  # noqa: BLE001
                dt = time.perf_counter() - t0
                rows.append({"i": i, "article": a["title"][:70], "seconds": round(dt, 2),
                             "parseable": False, "schema_ok": False,
                             "error": f"{type(e).__name__}: {e}", "result": None})
                print(f"  {i + 1:>3}/{args.n}  {dt:>7.2f}s  ERR  {type(e).__name__}: {e}")

    ok_n = sum(1 for r in rows if r["schema_ok"])
    parse_n = sum(1 for r in rows if r["parseable"])
    summary = {
        "model": args.model, "n": args.n,
        "schema_valid": ok_n, "parseable": parse_n,
        "latency": {
            "p50": round(statistics.median(lat), 2) if lat else None,
            "p95": round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 2) if lat else None,
            "min": round(min(lat), 2) if lat else None,
            "max": round(max(lat), 2) if lat else None,
            "mean": round(statistics.fmean(lat), 2) if lat else None,
        },
        "rows": rows,
    }
    tag = args.model.replace(":", "_")
    (OUT / f"bench_{tag}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    L = summary["latency"]
    print(f"\n  {args.model}")
    print(f"  schema-valid : {ok_n}/{args.n}   parseable: {parse_n}/{args.n}")
    print(f"  latency      : p50={L['p50']}s  p95={L['p95']}s  "
          f"min={L['min']}s  max={L['max']}s  mean={L['mean']}s")
    print(f"  -> {OUT / f'bench_{tag}.json'}")


# ---------------------------------------------------------------- concurrency

async def _one(client, url, payload):
    t0 = time.perf_counter()
    try:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return time.perf_counter() - t0, None
    except Exception as e:  # noqa: BLE001
        return time.perf_counter() - t0, f"{type(e).__name__}: {e}"


async def _run_conc(model, n, timeout):
    arts = json.loads((OUT / "sample_articles.json").read_text(encoding="utf-8"))
    url = f"{base_url()}/api/chat"
    payloads = [{
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(
            title=a["title"], source=a["source"], text=a["text"][:8000])}],
        "stream": False, "format": SCHEMA, "options": {"temperature": 0},
    } for a in (arts[i % len(arts)] for i in range(n))]

    async with httpx.AsyncClient(timeout=timeout) as c:
        t0 = time.perf_counter()
        res = await asyncio.gather(*[_one(c, url, p) for p in payloads])
        return time.perf_counter() - t0, res


def cmd_concurrency(args):
    out = {}
    print(f"model={args.model}\n")
    for n in (1, 2, 4, 8):
        wall, res = asyncio.run(_run_conc(args.model, n, args.timeout))
        errs = [e for _, e in res if e]
        per = [d for d, e in res if not e]
        out[n] = {"wall_seconds": round(wall, 2),
                  "mean_request_seconds": round(statistics.fmean(per), 2) if per else None,
                  "errors": errs}
        speedup = (out[1]["wall_seconds"] * n / wall) if 1 in out and wall else 1.0
        print(f"  n={n:<2} wall={wall:>7.2f}s  mean/req="
              f"{out[n]['mean_request_seconds']}s  errors={len(errs)}  "
              f"effective_speedup={speedup:.2f}x")
        if errs:
            print(f"       first error: {errs[0][:120]}")

    (OUT / "concurrency.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n  -> {OUT / 'concurrency.json'}")
    print("  Interpretation: speedup ~1.0x means the server serialises requests.")


# ---------------------------------------------------------------- long prompt

def cmd_longprompt(args):
    arts = json.loads((OUT / "sample_articles.json").read_text(encoding="utf-8"))
    blob = ("\n\n".join(a["text"] for a in arts) * 6)[:50000]
    print(f"model={args.model}  prompt={len(blob)} chars  timeout={args.timeout}s")
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": PROMPT.format(
            title="Long prompt stress test", source="synthetic", text=blob)}],
        "stream": False, "format": SCHEMA, "options": {"temperature": 0},
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{base_url()}/api/chat", json=payload, timeout=args.timeout)
        dt = time.perf_counter() - t0
        r.raise_for_status()
        body = r.json()
        ok, err = _validate(json.loads(body["message"]["content"]))
        result = {"chars": len(blob), "seconds": round(dt, 2), "completed": True,
                  "schema_ok": ok, "error": err,
                  "prompt_eval_count": body.get("prompt_eval_count"),
                  "eval_count": body.get("eval_count")}
        print(f"  COMPLETED in {dt:.2f}s  schema_ok={ok}  "
              f"prompt_tokens={body.get('prompt_eval_count')}")
    except Exception as e:  # noqa: BLE001
        dt = time.perf_counter() - t0
        result = {"chars": len(blob), "seconds": round(dt, 2), "completed": False,
                  "error": f"{type(e).__name__}: {e}"}
        print(f"  FAILED after {dt:.2f}s: {type(e).__name__}: {e}")

    (OUT / "longprompt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tags").set_defaults(fn=cmd_tags)
    sub.add_parser("fetch").set_defaults(fn=cmd_fetch)

    b = sub.add_parser("bench"); b.set_defaults(fn=cmd_bench)
    b.add_argument("--model", required=True)
    b.add_argument("--n", type=int, default=20)
    b.add_argument("--timeout", type=float, default=300)

    c = sub.add_parser("concurrency"); c.set_defaults(fn=cmd_concurrency)
    c.add_argument("--model", required=True)
    c.add_argument("--timeout", type=float, default=300)

    lp = sub.add_parser("longprompt"); lp.set_defaults(fn=cmd_longprompt)
    lp.add_argument("--model", required=True)
    lp.add_argument("--timeout", type=float, default=600)

    args = p.parse_args()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    args.fn(args)
