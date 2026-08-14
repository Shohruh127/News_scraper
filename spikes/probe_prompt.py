"""M0.1b - does adding enum definitions fix the categorical errors?

Baseline prompt (probe_ollama.PROMPT) defines the numeric dimensions but gives no
definition for any of the 12 topics or 6 maturity levels. Hypothesis: the model's
reasoning is strong but its enum choice is unguided.

Usage:  python probe_prompt.py --model gemma4:latest
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from probe_ollama import OUT, PROMPT, SCHEMA, _validate, base_url  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROMPT_V2 = """You are a technical editor for an AI-engineering news digest read by
engineers and technical decision-makers.

Classify the article below. Return JSON only.

## primary_topic — choose the SINGLE best fit

- frontier_models: a new or updated LLM/foundation model, its weights, API, or capabilities
- ai_agents: agent frameworks, tool calling, MCP/A2A, multi-agent systems, coding/browser agents
- new_approaches: a new architecture, training method, inference technique, or algorithm
- speech_voice: STT, TTS, voice agents, diarization, audio models
- robotics: physical robots, embodied AI, control policies
- fintech: financial technology, payments, banking systems
- govtech: government digital services, public administration systems
- production_engineering: infrastructure, serving, deployment tooling, developer tools, releases of such tools
- startups: a company launching or shipping a deployed product
- technical_talks: conference talks, demos, recorded technical presentations
- safety_security: alignment, jailbreaks, model security, agent permissions, red-teaming
- irrelevant: EVERYTHING ELSE — executive appointments, funding rounds, partnerships,
  marketing, opinion pieces, consumer gadgets, general business news

If the article contains no technical substance, primary_topic MUST be "irrelevant".
Do not force a technical category onto a business story.

## maturity — what actually exists right now

- production_deployment: running in a real organisation, with reported results
- live_product: publicly usable product or API available today
- reproducible_open_source: code or weights published and installable
- public_pilot: limited preview, waitlist, or restricted access
- announcement_only: announced, but nothing usable is released yet
- paper_only: a research paper or preprint, no released artifacts

An arXiv paper is paper_only even if the results are excellent.
A changelog for an existing tool is live_product, not production_deployment.

## relevant

true only if BOTH: (a) primary_topic is not "irrelevant", AND (b) an engineer could act
on this — read code, install something, evaluate a model, or change a technical decision.
An article you would not put in an engineering digest is relevant=false.

## Numeric scores

- novelty: 1 = rehash of known news, 10 = genuinely new capability or result
- evidence: 1 = vendor claim only, 10 = reproducible artifacts (weights, repo, independent eval)
- production_readiness: 1 = paper or announcement, 10 = deployed and documented

ARTICLE
Title: {title}
Source: {source}
---
{text}
"""


def run(model, prompt_template, label, arts, timeout):
    rows = []
    with httpx.Client(timeout=timeout) as c:
        for a in arts:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt_template.format(
                    title=a["title"], source=a["source"], text=a["text"][:8000])}],
                "stream": False, "format": SCHEMA, "options": {"temperature": 0},
            }
            t0 = time.perf_counter()
            try:
                r = c.post(f"{base_url()}/api/chat", json=payload)
                dt = time.perf_counter() - t0
                r.raise_for_status()
                parsed = json.loads(r.json()["message"]["content"])
                ok, err = _validate(parsed)
                rows.append({"title": a["title"], "seconds": round(dt, 2),
                             "schema_ok": ok, "error": err, "result": parsed})
            except Exception as e:  # noqa: BLE001
                rows.append({"title": a["title"], "seconds": round(time.perf_counter() - t0, 2),
                             "schema_ok": False, "error": str(e), "result": None})
            print(f"  [{label}] {rows[-1]['seconds']:>6.2f}s  "
                  f"{(rows[-1]['result'] or {}).get('primary_topic', 'ERR'):<22} "
                  f"{a['title'][:52]}")
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--timeout", type=float, default=300)
    args = p.parse_args()

    arts = json.loads((OUT / "sample_articles.json").read_text(encoding="utf-8"))

    print("=== V1 baseline (no enum definitions) ===")
    v1 = run(args.model, PROMPT, "v1", arts, args.timeout)
    print("\n=== V2 (enum definitions + irrelevant default) ===")
    v2 = run(args.model, PROMPT_V2, "v2", arts, args.timeout)

    print(f"\n{'ARTICLE':<50} {'V1 TOPIC':<22} {'V2 TOPIC':<22} {'V1 MAT':<24} V2 MAT")
    print("-" * 150)
    changed = 0
    for a, b in zip(v1, v2):
        ra, rb = a["result"] or {}, b["result"] or {}
        mark = " *" if ra.get("primary_topic") != rb.get("primary_topic") else "  "
        if mark == " *":
            changed += 1
        print(f"{a['title'][:48]:<50} {ra.get('primary_topic', '?'):<22} "
              f"{rb.get('primary_topic', '?'):<22} {ra.get('maturity', '?'):<24} "
              f"{rb.get('maturity', '?')}{mark}")

    v1r = sum(1 for r in v1 if (r['result'] or {}).get('relevant'))
    v2r = sum(1 for r in v2 if (r['result'] or {}).get('relevant'))
    print(f"\ntopic changed: {changed}/{len(v1)}")
    print(f"relevant=true: v1={v1r}/{len(v1)}   v2={v2r}/{len(v2)}")
    print(f"schema ok    : v1={sum(1 for r in v1 if r['schema_ok'])}/{len(v1)}   "
          f"v2={sum(1 for r in v2 if r['schema_ok'])}/{len(v2)}")
    print(f"mean latency : v1={sum(r['seconds'] for r in v1) / len(v1):.2f}s   "
          f"v2={sum(r['seconds'] for r in v2) / len(v2):.2f}s")

    (OUT / "prompt_ab.json").write_text(
        json.dumps({"model": args.model, "v1": v1, "v2": v2}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"-> {OUT / 'prompt_ab.json'}")
