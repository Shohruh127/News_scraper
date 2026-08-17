"""Shared test helpers."""

from apps.digest.models import Analysis


def make_editorial(article, *, summary_uz="Yangi model nashr qilindi.", built="Model",
                   limitations="GPU required", local_deployable=True, model_tag="mimo-v2.5"):
    """Create the two editorial analyses a renderable DigestItem needs (ADR-005).

    Rendering reads `editorial_uz` for reader-facing Uzbek and `editorial_en` for the
    technical appendix, so a test that creates only one of them renders a half-built post.
    """
    en = Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.EDITORIAL_EN,
        model_tag=model_tag,
        payload={
            "headline_en": "A new model was released",
            "summary_en": "A new model was released with open weights.",
            "why_it_matters_en": "It can be self-hosted.",
            "leadership_en": "Reduces API dependency.",
            "uzbekistan_application_en": "Local teams can self-host it.",
            "technical": {
                "what_was_built": built,
                "limitations": limitations,
                "local_deployable": local_deployable,
            },
            "evidence_level": "vendor_claim_only",
        },
        latency_ms=8000,
    )
    uz = Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag=model_tag,
        payload={
            "headline_uz": "Yangi model chiqdi",
            "summary_uz": summary_uz,
            "why_it_matters_uz": "Muhim yangilik.",
            "leadership_uz": "Boshqaruv uchun tavsiya.",
            "uzbekistan_application_uz": "O'zbekistonda qo'llash mumkin.",
        },
        latency_ms=6000,
    )
    return en, uz
