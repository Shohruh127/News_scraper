"""One story, one slot: Tier B same-story groups (clustering.py, llm.group_same_story).

The case is the evening of 2026-09-09: DeepMind's own AlphaGenome Atlas post (12k
characters, no photo) and Google's blog post about it via HN (2.7k, photo). Jaccard 0.215
against a threshold of 0.80, so Tier A saw two stories; they took positions 1 and 2 of the
block and position 1 then failed for want of a photo.
"""

import pytest

from apps.digest import clustering, llm, ranking
from apps.digest.llm import ChatResult
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def outlets(db):
    own = Source.objects.create(
        name="deepmind", connector="rss", url="https://deepmind.example", priority=90
    )
    hn = Source.objects.create(name="hn", connector="hn", url="https://hn.example", priority=40)
    return own, hn


def make(source, url, title, text, n, *, topic="frontier_models", novelty=8, image=None):
    art = Article.objects.create(
        source=source,
        canonical_url=url,
        content_hash=f"same{n:060d}",
        title=title,
        extracted_text=text,
        status=Article.Status.CLASSIFIED,
        meta={"image_url": image} if image else {},
    )
    an = Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": topic,
            "maturity": "live_product",
            "novelty": novelty,
            "evidence": 8,
            "production_readiness": 6,
            "audience": 6,
            "reason": "x",
        },
        latency_ms=1000,
    )
    return art, an


OWN_POST = (
    "Today we are introducing AlphaGenome Atlas, a database that predicts the effects of "
    "every possible single nucleotide variant in the human genome. Scientists understand "
    "the two percent of the genome that codes for proteins relatively well. " * 8
)
BLOG_POST = (
    "Google DeepMind has released a high-resolution map of human DNA. The AlphaGenome "
    "Atlas is available today through a website portal that requires zero coding. " * 8
)
OTHER = (
    "LG smart TVs are almost constantly logging and uploading data about owners and their "
    "homes, even when offline or on standby, according to a new report. " * 8
)


@pytest.fixture
def alphagenome(outlets):
    own, hn = outlets
    a, an_a = make(own, "https://deepmind.example/atlas", "AlphaGenome Atlas: a map", OWN_POST, 1)
    b, an_b = make(
        hn,
        "https://blog.example/atlas",
        "AlphaGenome Atlas: high-resolution map of DNA",
        BLOG_POST,
        2,
        novelty=7,
        image="https://blog.example/atlas.jpg",
    )
    c, an_c = make(
        hn, "https://verge.example/lg", "LG TVs caught spying", OTHER, 3, topic="safety_security"
    )
    return (a, an_a), (b, an_b), (c, an_c)


def test_text_alone_sees_two_stories(alphagenome):
    (a, an_a), (b, an_b), _ = alphagenome
    assert len(clustering.cluster_candidates([(a, an_a, 0.82), (b, an_b, 0.80)])) == 2


def test_a_judged_group_merges_what_text_cannot(alphagenome):
    (a, an_a), (b, an_b), (c, an_c) = alphagenome
    result = clustering.cluster_candidates(
        [(a, an_a, 0.82), (b, an_b, 0.80), (c, an_c, 0.78)],
        same_story_groups=[[a.id, b.id]],
    )
    assert [r[0].id for r in result] == [b.id, c.id]
    primary, analysis, score, secondary = result[0]
    assert primary.id == b.id, "the copy with the photo is the one that can be posted"
    assert analysis.article_id == b.id
    assert score == 0.82, "the story keeps its best score"
    assert [s.id for s in secondary] == [a.id]


def test_unknown_ids_and_singletons_change_nothing(alphagenome):
    (a, an_a), (b, an_b), _ = alphagenome
    cands = [(a, an_a, 0.82), (b, an_b, 0.80)]
    assert len(clustering.cluster_candidates(cands, same_story_groups=[[a.id, 999999]])) == 2
    assert len(clustering.cluster_candidates(cands, same_story_groups=[[a.id]])) == 2
    assert len(clustering.cluster_candidates(cands, same_story_groups=[])) == 2


def test_a_group_touching_a_text_cluster_merges_the_whole_cluster(alphagenome, outlets):
    (a, an_a), (b, an_b), _ = alphagenome
    own, _ = outlets
    a2, an_a2 = make(own, "https://deepmind.example/atlas-copy", "AlphaGenome Atlas", OWN_POST, 4)
    result = clustering.cluster_candidates(
        [(a, an_a, 0.82), (a2, an_a2, 0.81), (b, an_b, 0.80)],
        same_story_groups=[[a2.id, b.id]],
    )
    assert len(result) == 1
    assert {s.id for s in result[0][3]} == {a.id, a2.id}


def test_group_same_story_reads_the_answer_and_drops_what_is_not_a_candidate(
    alphagenome, monkeypatch
):
    (a, an_a), (b, an_b), (c, an_c) = alphagenome
    seen = {}

    def fake(tier, prompt, schema, num_predict, client=None):
        seen.update(tier=tier, prompt=prompt, schema=schema)
        return ChatResult(
            payload={"groups": [[b.id, a.id, 424242], [c.id]]},
            latency_ms=12,
            model_tag="smart",
            input_tokens=300,
            output_tokens=20,
        )

    monkeypatch.setattr(llm, "classifier_chat", fake)
    groups = llm.group_same_story([(a, an_a, 0.82), (b, an_b, 0.80), (c, an_c, 0.78)])
    assert groups == [[a.id, b.id]]
    assert seen["tier"] == llm.TIER_DEEP
    assert seen["schema"] is llm.STORY_GROUPS_SCHEMA
    assert f"{a.id} | deepmind | AlphaGenome Atlas: a map |" in seen["prompt"]
    assert "LG TVs caught spying" in seen["prompt"]


def test_group_same_story_failure_means_no_groups(alphagenome, monkeypatch):
    (a, an_a), (b, an_b), _ = alphagenome

    def boom(**kwargs):
        raise RuntimeError("gateway 503")

    monkeypatch.setattr(llm, "classifier_chat", boom)
    assert llm.group_same_story([(a, an_a, 0.82), (b, an_b, 0.80)]) == []
    assert llm.group_same_story([(a, an_a, 0.82)]) == [], "one candidate is not a question"


def test_selection_gives_one_story_one_slot(alphagenome, settings, monkeypatch):
    (a, _), (b, _), (c, _) = alphagenome
    settings.STORY_GROUPING_ENABLED = True
    asked = []

    def fake_groups(candidates, client=None):
        asked.append([art.id for art, _, _ in candidates])
        return [[a.id, b.id]]

    monkeypatch.setattr(llm, "group_same_story", fake_groups)
    selected = ranking.select_digest_candidates()
    assert asked and set(asked[0]) == {a.id, b.id, c.id}
    assert [art.id for art, _, _, _ in selected] == [b.id, c.id]
    assert [s.id for s in selected[0][3]] == [a.id]


def test_selection_skips_the_call_when_disabled(alphagenome, settings, monkeypatch):
    (a, _), (b, _), (c, _) = alphagenome
    settings.STORY_GROUPING_ENABLED = False

    def never(candidates, client=None):
        raise AssertionError("grouping must not be called when disabled")

    monkeypatch.setattr(llm, "group_same_story", never)
    assert len(ranking.select_digest_candidates()) == 3
