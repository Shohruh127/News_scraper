"""M0.4 - how much content actually arrives per day, and build the gold set.

Answers: is this a daily product or a twice-weekly one? And produces the labelled set
that measures the classifier in T1.5.

Usage:  python probe_volume.py
"""

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import httpx
import trafilatura
from dateutil import parser as dateparser

sys.path.insert(0, str(Path(__file__).parent))
from probe_ollama import OUT  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAYS = 3
CUTOFF = datetime.now(timezone.utc) - timedelta(days=DAYS)
UA = {"User-Agent": "news-radar-spike/0.1 (evaluation)"}
TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref|mc_cid|mc_eid)")


def canonical(url: str) -> str:
    p = urlparse(url)
    q = "&".join(kv for kv in p.query.split("&") if kv and not TRACKING.match(kv.split("=")[0]))
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", q, ""))


def parse_date(value):
    if not value:
        return None
    try:
        d = dateparser.parse(str(value))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def extract(url):
    try:
        html = trafilatura.fetch_url(url)
        return (trafilatura.extract(html, include_comments=False) or "") if html else ""
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ sources

def src_rss(client, name, url):
    feed = feedparser.parse(client.get(url).text)
    items = []
    for e in feed.entries:
        d = parse_date(e.get("published") or e.get("updated"))
        items.append({"source": name, "url": e.link, "title": e.title, "published_at": d})
    return items


def src_anthropic(client, name, url):
    """Anthropic publishes no RSS. Pull the news listing directly."""
    html = client.get(url).text
    hrefs = set(re.findall(r'href="(/news/[^"#?]+)"', html))
    items = []
    for h in sorted(hrefs):
        full = urljoin(url, h)
        items.append({"source": name, "url": full,
                      "title": h.rsplit("/", 1)[-1].replace("-", " ").title(),
                      "published_at": None})
    return items


def src_hf(client, name, url):
    items = []
    for p in client.get(url).json():
        pp = p.get("paper", p)
        items.append({
            "source": name,
            "url": f"https://huggingface.co/papers/{pp.get('id', '')}",
            "title": pp.get("title", ""),
            "published_at": parse_date(p.get("publishedAt") or pp.get("publishedAt")),
            "text": f"{pp.get('title', '')}\n\n{pp.get('summary', '')}",
        })
    return items


def src_github(client, name, url):
    items = []
    for rel in client.get(url).json():
        items.append({"source": name, "url": rel["html_url"],
                      "title": f"{url.split('/repos/')[1].split('/releases')[0]} {rel['name']}",
                      "published_at": parse_date(rel.get("published_at")),
                      "text": f"{rel['name']}\n\n{rel.get('body') or ''}"})
    return items


def src_hn(client, name, url):
    items = []
    for h in client.get(url).json().get("hits", []):
        if not h.get("url"):
            continue
        items.append({"source": name, "url": h["url"], "title": h.get("title", ""),
                      "published_at": parse_date(h.get("created_at")),
                      "meta": {"points": h.get("points")}})
    return items


SOURCES = [
    ("openai", src_rss, "https://openai.com/news/rss.xml"),
    ("deepmind", src_rss, "https://deepmind.google/blog/feed/basic/"),
    ("anthropic", src_anthropic, "https://www.anthropic.com/news"),
    ("hf_papers", src_hf, "https://huggingface.co/api/daily_papers?limit=60"),
    ("gh_langgraph", src_github,
     "https://api.github.com/repos/langchain-ai/langgraph/releases?per_page=10"),
    ("gh_mcp", src_github,
     "https://api.github.com/repos/modelcontextprotocol/servers/releases?per_page=10"),
    ("gh_ollama", src_github,
     "https://api.github.com/repos/ollama/ollama/releases?per_page=10"),
    ("hn", src_hn,
     "https://hn.algolia.com/api/v1/search_by_date?tags=story&numericFilters=points%3E50&hitsPerPage=60"),
]


def main():
    raw, health = [], {}
    with httpx.Client(timeout=45, follow_redirects=True, headers=UA) as c:
        for name, fn, url in SOURCES:
            try:
                items = fn(c, name, url)
                health[name] = {"ok": True, "raw_items": len(items), "error": None}
                raw.extend(items)
                print(f"  {name:<14} {len(items):>3} items")
            except Exception as e:  # noqa: BLE001
                health[name] = {"ok": False, "raw_items": 0, "error": f"{type(e).__name__}: {e}"}
                print(f"  {name:<14} FAILED  {type(e).__name__}: {e}")

        # Window to the last N days. Undated items are kept and flagged.
        windowed = [i for i in raw
                    if i["published_at"] is None or i["published_at"] >= CUTOFF]
        undated = sum(1 for i in windowed if i["published_at"] is None)
        print(f"\n  {len(raw)} raw -> {len(windowed)} within {DAYS}d ({undated} undated)")

        # Fetch text where the source did not already provide it.
        print("\n  extracting text...")
        for i, it in enumerate(windowed, 1):
            if not it.get("text"):
                it["text"] = extract(it["url"])
            if i % 20 == 0:
                print(f"    {i}/{len(windowed)}")

    # Dedup: canonical url, then content hash.
    seen_url, seen_hash, unique, dup_url, dup_hash, too_short = set(), set(), [], 0, 0, 0
    for it in windowed:
        cu = canonical(it["url"])
        if cu in seen_url:
            dup_url += 1
            continue
        seen_url.add(cu)
        text = (it.get("text") or "").strip()
        if len(text) < 400:
            too_short += 1
            continue
        h = hashlib.sha256(text.encode()).hexdigest()
        if h in seen_hash:
            dup_hash += 1
            continue
        seen_hash.add(h)
        it["canonical_url"], it["content_hash"] = cu, h
        unique.append(it)

    per_source = Counter(i["source"] for i in unique)
    per_day = defaultdict(Counter)
    for i in unique:
        day = i["published_at"].date().isoformat() if i["published_at"] else "undated"
        per_day[day][i["source"]] += 1

    # Gold set: spread across sources, cap per source so one feed cannot dominate.
    gold, per_src_count = [], Counter()
    for it in sorted(unique, key=lambda x: (x["source"], x["title"])):
        if per_src_count[it["source"]] >= 5 or len(gold) >= 30:
            continue
        per_src_count[it["source"]] += 1
        gold.append({
            "id": it["content_hash"][:12],
            "url": it["url"],
            "title": it["title"],
            "source": it["source"],
            "published_at": it["published_at"].isoformat() if it["published_at"] else None,
            "text_excerpt": it["text"][:1500],
            "human_label": None,
            "human_topic": None,
            "human_maturity": None,
            "human_note": None,
        })

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    with (data_dir / "gold_set.jsonl").open("w", encoding="utf-8") as f:
        for g in gold:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- report
    L = ["# M0.4 — Content Volume and Gold Set\n",
         f"Date: 2026-08-14  \nWindow: last {DAYS} days  \n"
         "Generated by `spikes/probe_volume.py`\n",
         "## 1. Source reliability\n",
         "| Source | Fetch | Raw items | Error |", "|---|---|---|---|"]
    for name, h in health.items():
        L.append(f"| `{name}` | {'ok' if h['ok'] else '**FAILED**'} | {h['raw_items']} | "
                 f"{h['error'] or ''} |")

    L += ["\n## 2. Funnel\n", "| Stage | Count |", "|---|---|",
          f"| Raw items | {len(raw)} |",
          f"| Within {DAYS}-day window | {len(windowed)} |",
          f"| Duplicate URL | -{dup_url} |",
          f"| Duplicate content hash | -{dup_hash} |",
          f"| Text under 400 chars | -{too_short} |",
          f"| **Unique usable** | **{len(unique)}** |",
          f"\nDuplicate rate: {(dup_url + dup_hash) / max(len(windowed), 1) * 100:.1f}%  ",
          f"Extraction failure rate: {too_short / max(len(windowed), 1) * 100:.1f}%  ",
          f"Undated items: {undated}\n",
          "## 3. Unique items per source\n", "| Source | Items | Per day |", "|---|---|---|"]
    for s, n in per_source.most_common():
        L.append(f"| `{s}` | {n} | {n / DAYS:.1f} |")
    L.append(f"| **Total** | **{len(unique)}** | **{len(unique) / DAYS:.1f}** |")

    L += ["\n## 4. Per day\n", "| Day | " + " | ".join(per_source) + " | Total |",
          "|---" * (len(per_source) + 2) + "|"]
    for day in sorted(per_day):
        row = per_day[day]
        L.append(f"| {day} | " + " | ".join(str(row.get(s, 0)) for s in per_source)
                 + f" | {sum(row.values())} |")

    L += ["\n## 5. Does a daily digest have enough material?\n",
          f"Unique items reaching the classifier: **{len(unique) / DAYS:.1f} per day**.\n",
          "That is the input, not the output. The strict filter still removes "
          "`announcement_only` and `paper_only`, everything scoring low, and anything "
          "off-topic. The realistic published count is a fraction of this.\n",
          "**This number is confirmed only after the gold set is labelled** — the human "
          "labels reveal what proportion would actually pass.\n",
          "## 6. Gold set\n",
          f"`data/gold_set.jsonl` — **{len(gold)} items**, "
          f"{len(per_src_count)} sources, max 5 per source.\n",
          "| Source | Items |", "|---|---|"]
    for s, n in per_src_count.most_common():
        L.append(f"| `{s}` | {n} |")

    L += ["\n### How to label\n",
          "Fill four fields on every line of `data/gold_set.jsonl`:\n",
          "| Field | Values |", "|---|---|",
          "| `human_label` | `keep` or `drop` — would this belong in the digest? |",
          "| `human_topic` | one `primary_topic` from `CONTENT_SCHEMA.md` §2 |",
          "| `human_maturity` | one `maturity` from `CONTENT_SCHEMA.md` §3 |",
          "| `human_note` | optional, only when the call was difficult |",
          "\nLabel from the title and `text_excerpt`. Judge as an editor, not as a "
          "classifier — `human_label` answers whether you would publish it.\n",
          "This set is the acceptance criterion for T1.5 (**precision ≥ 0.80**). "
          "Without labels that gate cannot be measured and GATE 1 cannot pass.\n"]

    path = Path(__file__).parent.parent / "docs" / "spike" / "CONTENT_VOLUME.md"
    path.write_text("\n".join(L), encoding="utf-8")
    (OUT / "volume_raw.json").write_text(
        json.dumps({"health": health, "unique": len(unique),
                    "per_source": dict(per_source)}, indent=2), encoding="utf-8")

    print(f"\n  unique usable : {len(unique)}  ({len(unique) / DAYS:.1f}/day)")
    print(f"  gold set      : {len(gold)} items -> data/gold_set.jsonl")
    print(f"  report        : {path}")


if __name__ == "__main__":
    main()
