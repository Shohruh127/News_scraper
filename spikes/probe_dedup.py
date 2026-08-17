"""M0.5 - which similarity signal separates real duplicates from distinct releases?

Compares three signals on the 185 live articles:
  - title Jaccard on word shingles      (what we tried and removed)
  - TEXT Jaccard on char 5-gram shingles (MinHash/LSH measures this, exactly)
  - TEXT Jaccard on word 3-gram shingles

MinHash is an *approximation* of Jaccard for scale. At 185 articles exact Jaccard is
cheap (17k pairs), so measure the signal quality first and worry about indexing later.

Two cases decide it:
  MUST merge:  Qwen3.8-2.4T-A95B-FP8  vs  Qwen3.8-2.4T-A95B   (same model card boilerplate)
  MUST NOT:    ollama v0.32.10        vs  ollama v0.32.11      (different changelogs)
"""

import re
import sys
from itertools import combinations
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).parent.parent))
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.digest.models import Article  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower()).strip()


def char_shingles(t: str, k: int = 5) -> set[str]:
    t = norm(t)
    return {t[i : i + k] for i in range(max(0, len(t) - k + 1))}


def word_shingles(t: str, k: int = 3) -> set[str]:
    w = norm(t).split()
    return {" ".join(w[i : i + k]) for i in range(max(0, len(w) - k + 1))}


def jac(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


SIGNALS = {
    "title_word2": lambda a: word_shingles(a.title, 2),
    "text_char5": lambda a: char_shingles(a.extracted_text[:6000], 5),
    "text_word3": lambda a: word_shingles(a.extracted_text[:6000], 3),
}


def main():
    arts = list(Article.objects.select_related("source").order_by("id"))
    print(f"{len(arts)} articles, {len(arts) * (len(arts) - 1) // 2} pairs\n")

    sets = {name: {a.id: fn(a) for a in arts} for name, fn in SIGNALS.items()}
    by_id = {a.id: a for a in arts}

    # The two decisive pairs.
    qwen = [a.id for a in arts if "Qwen3.8" in a.title]
    oll = sorted(
        (a.id for a in arts if a.title.startswith("ollama/ollama v0.32.1")),
        key=lambda i: by_id[i].title,
    )
    cases = []
    if len(qwen) >= 2:
        cases.append(("MUST merge   ", qwen[0], qwen[1]))
    if len(oll) >= 2:
        cases.append(("MUST NOT     ", oll[0], oll[1]))

    print(f"{'case':<14} {'signal':<13} {'score':>7}")
    print("-" * 38)
    for label, i, j in cases:
        for name in SIGNALS:
            print(f"{label} {name:<13} {jac(sets[name][i], sets[name][j]):>7.3f}")
        print(f"{'':14} {by_id[i].title[:30]!r} ~ {by_id[j].title[:30]!r}")
        print()

    # Full sweep: how many pairs each signal merges, and how many are same-source.
    print(f"{'signal':<13} {'thresh':>7} {'pairs':>6} {'same-src':>9} {'cross-src':>10}")
    print("-" * 50)
    for name in SIGNALS:
        s = sets[name]
        for th in (0.60, 0.70, 0.80, 0.90):
            same = cross = 0
            hits = []
            for i, j in combinations(s, 2):
                if jac(s[i], s[j]) >= th:
                    if by_id[i].source_id == by_id[j].source_id:
                        same += 1
                    else:
                        cross += 1
                    hits.append((i, j))
            print(f"{name:<13} {th:>7.2f} {same + cross:>6} {same:>9} {cross:>10}")
            if name == "text_char5" and th == 0.80:
                print("     merged pairs at text_char5 >= 0.80:")
                for i, j in hits[:12]:
                    print(f"       {by_id[i].title[:36]!r} ~ {by_id[j].title[:36]!r}")
        print()


if __name__ == "__main__":
    main()
