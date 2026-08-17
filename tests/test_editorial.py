"""Tests for the editorial stage (T1.10) and Uzbek rendering rules."""

from datetime import date

import httpx
import pytest
import respx

from apps.digest import llm, ranking
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="test_editorial_src",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
        priority=85,
    )


@respx.mock
def test_editorial_analysis_generates_uzbek_and_technical(db, source, settings):
    settings.OLLAMA_BASE_URL = "http://localhost:11434"
    settings.OLLAMA_DEEP_MODEL = "gemma4:31b"

    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/editorial-art",
        content_hash="h_ed",
        title="Ollama Multi-GPU Serving Update",
        extracted_text="Ollama released multi-gpu support with vLLM engine integration.",
        status=Article.Status.CLASSIFIED,
    )

    fake_response = {
        "message": {
            "content": """{
                "summary_uz": "Ollama bir nechta GPU'da xizmat ko'rsata boshladi.",
                "why_it_matters_uz": "Modellarni tezroq ishga tushirish imkonini beradi.",
                "leadership_uz": "Infratuzilma xarajatlarini kamaytiradi.",
                "technical": {
                    "what_was_built": "Multi-GPU inference support",
                    "architecture": "vLLM backend wrapper",
                    "license": "MIT",
                    "repo_url": "https://github.com/ollama/ollama",
                    "api_url": "",
                    "hardware": "NVIDIA RTX 4090",
                    "install": "ollama serve --gpus all",
                    "benchmarks": "2.4x throughput",
                    "limitations": "Linux only",
                    "local_deployable": true
                },
                "uzbekistan_application_uz": "Mahalliy serverlarda modellarni deploy qilish.",
                "evidence_level": "vendor_claim_only"
            }"""
        }
    }

    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "gemma4:31b", "digest": "digest31b"}]}
        )
    )
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=fake_response)
    )

    analyses = llm.analyse_for_digest_logic([art.id])
    assert len(analyses) == 1
    analysis = analyses[0]

    assert analysis.stage == Analysis.Stage.EDITORIAL
    assert (
        analysis.payload["summary_uz"]
        == "Ollama bir nechta GPU'da xizmat ko'rsata boshladi."
    )
    assert analysis.payload["technical"]["local_deployable"] is True


def test_rendering_requires_editorial_summary_uz(db, source):
    """Rendering must fail with ValueError if editorial analysis or summary_uz is missing."""
    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/missing-uz",
        content_hash="h_missing",
        title="Article Without Uzbek Summary",
        extracted_text="Content",
        status=Article.Status.CLASSIFIED,
    )
    # Only classification analysis, no editorial analysis
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
            "reason": "English reason only",
        },
        latency_ms=1000,
    )

    digest = Digest.objects.create(
        digest_date=date(2026, 8, 14),
        status=Digest.Status.COMPOSED,
    )
    DigestItem.objects.create(
        digest=digest,
        article=art,
        position=1,
        score=0.85,
    )

    with pytest.raises(ValueError, match="English fallback is prohibited"):
        ranking.render_channel_post(digest)


def test_rendering_succeeds_with_editorial_analysis(db, source):
    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/with-uz",
        content_hash="h_with_uz",
        title="Valid Uzbek Article",
        extracted_text="Content",
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
        latency_ms=1000,
    )
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.EDITORIAL,
        model_tag="gemma4:31b",
        payload={
            "summary_uz": "Yangi arxitektura muvaffaqiyatli sinovdan o'tdi.",
            "why_it_matters_uz": "Tezlik 3 barobar oshdi.",
            "leadership_uz": "Qaror qabul qiluvchilar uchun muhim.",
            "technical": {
                "what_was_built": "Fast transformer layer",
                "limitations": "Memory bounds",
                "local_deployable": True,
            },
            "uzbekistan_application_uz": "O'zbekistonda tadqiqotlar uchun mos.",
            "evidence_level": "vendor_claim_only",
        },
        latency_ms=2000,
    )

    digest = Digest.objects.create(
        digest_date=date(2026, 8, 14),
        status=Digest.Status.COMPOSED,
    )
    DigestItem.objects.create(
        digest=digest,
        article=art,
        position=1,
        score=0.85,
    )

    channel_post = ranking.render_channel_post(digest)
    assert "Yangi arxitektura muvaffaqiyatli sinovdan o'tdi." in channel_post
    assert "English reason" not in channel_post

    group_comment = ranking.render_group_comment(digest)
    assert "Fast transformer layer" in group_comment
    assert "Mavjud ✅" in group_comment
