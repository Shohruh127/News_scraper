"""Apply labels to data/gold_set.jsonl. Labels are keyed by the item id.

These labels were produced by Claude, not by a human editor. See
docs/spike/GOLD_SET_REVIEW.md for what that means for the T1.5 measurement.
"""

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# title fragment -> (label, topic, maturity, note)
LABELS = {
    "Anthropic Economic Index Connector": (
        "keep", "ai_agents", "live_product",
        "Borderline. A shipped MCP-style connector, so ai_agents fits; but it is also a "
        "data-PR piece and a stricter editor would call it irrelevant."),
    "Claude Opus 5": (
        "keep", "frontier_models", "live_product",
        "API available today, weights closed — live_product, not reproducible_open_source."),
    "Claude Sonnet 5": (
        "keep", "frontier_models", "live_product", ""),
    "Cognizant Anthropic": (
        "drop", "irrelevant", "announcement_only",
        "Business partnership, no technical substance."),
    "Donation Public First Action": (
        "drop", "irrelevant", "announcement_only", "Policy donation."),
    "Introducing Gemini 3.7 Flash": (
        "keep", "frontier_models", "live_product", ""),
    "Putting sign language AI": (
        "keep", "new_approaches", "live_product",
        "Taxonomy edge: sign language is video-to-text, so speech_voice does not apply "
        "despite being a translation model. Filed under new_approaches."),
    "langgraph==1.2.11": (
        "drop", "production_engineering", "live_product",
        "Patch release: dependency bumps and one minor flag. Correct topic, too small "
        "to publish."),
    "ollama/ollama v0.32.10": (
        "drop", "production_engineering", "live_product",
        "Patch release. The repeat_penalty default change does affect anyone running "
        "Ollama, so this is the weakest drop in the set."),
    "ollama/ollama v0.32.11": (
        "keep", "production_engineering", "live_product",
        "Adds DeepSeek Harness and Muse Code agent support — substantive, not a patch."),
    "ollama/ollama v0.32.9": (
        "keep", "frontier_models", "reproducible_open_source",
        "Arrives as an Ollama changelog but the news is an open 30B MoE model that runs "
        "today. Substance decides the topic, not the source."),
    "AI4AI at Test-Time": (
        "drop", "new_approaches", "paper_only", ""),
    "AVA-Encoder": (
        "drop", "new_approaches", "paper_only",
        "About video representation for agents; still a method paper, so new_approaches "
        "rather than ai_agents."),
    "Alaya-EVOKE": (
        "drop", "new_approaches", "paper_only", ""),
    "Are You Sure You're Sure?": (
        "drop", "new_approaches", "paper_only",
        "Studies model confidence, not model security — new_approaches, not "
        "safety_security."),
    "AutoDesign": (
        "drop", "new_approaches", "paper_only",
        "Hardest topic call in the set. Genuinely about agent harnesses, but it is a "
        "method paper with no released framework, so the research-paper default wins."),
    "Judge orders Google": (
        "drop", "irrelevant", "announcement_only", "Antitrust litigation, not AI."),
    "AI At Home Part 1": (
        "drop", "production_engineering", "announcement_only",
        "Taxonomy gap: an experience-report blog post fits no maturity value well. "
        "Marked announcement_only because no artifact is offered. Part 1 of a series."),
    "AI Is Threatening Natural Resources": (
        "drop", "irrelevant", "announcement_only",
        "AI environmental impact. Real subject, outside the twelve topics."),
    "AI agents lie, cheat and steal": (
        "drop", "irrelevant", "announcement_only",
        "Subject is agent trustworthiness, form is magazine opinion. The 'opinion pieces "
        "are irrelevant' rule wins over the subject matter."),
    "Accelerating GPT-5.6 Sol Ultrafast": (
        "keep", "production_engineering", "public_pilot",
        "Same story as the OpenAI post below, from the hardware side. A clustering case "
        "for M2 — both are individually keep-worthy."),
    "From assistance to execution": (
        "drop", "irrelevant", "announcement_only",
        "Vendor adoption statistics, no technical content."),
    "How RingCentral builds AI-native work": (
        "drop", "production_engineering", "production_deployment",
        "Shows label and maturity are independent: genuinely deployed at a named company, "
        "but vendor-authored with no measurable results, so it is not published."),
    "OpenAI appoints Dali Rajic": (
        "drop", "irrelevant", "announcement_only", "Executive appointment."),
    "Previewing Ultrafast mode": (
        "keep", "production_engineering", "public_pilot",
        "News is serving speed, not model capability — hence production_engineering "
        "rather than frontier_models. Limited access, so public_pilot."),
    # Fragment kept ASCII-safe: the real title uses a typographic apostrophe and a
    # non-breaking hyphen in "GPT-5.6".
    "builder": (
        "keep", "ai_agents", "live_product",
        "Actionable technical guide: model selection, tool calling, multi-agent "
        "orchestration."),
}


def match(title: str):
    for fragment, value in LABELS.items():
        if fragment.lower() in title.lower():
            return value
    return None


def main():
    path = Path(__file__).parent.parent / "data" / "gold_set.jsonl"
    rows = [json.loads(line) for line in path.open(encoding="utf-8")]

    unmatched = []
    for row in rows:
        found = match(row["title"])
        if not found:
            unmatched.append(row["title"])
            continue
        label, topic, maturity, note = found
        row["human_label"] = label
        row["human_topic"] = topic
        row["human_maturity"] = maturity
        row["human_note"] = note
        row["labelled_by"] = "claude"

    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    keep = sum(1 for r in rows if r["human_label"] == "keep")
    print(f"labelled {len(rows) - len(unmatched)}/{len(rows)}")
    print(f"keep {keep}  drop {len(rows) - len(unmatched) - keep}")
    if unmatched:
        print("UNMATCHED:")
        for t in unmatched:
            print("  ", t)


if __name__ == "__main__":
    main()
