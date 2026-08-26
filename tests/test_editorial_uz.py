"""The single-stage Uzbek editorial (2026-08-26 design).

Phase 1 builds this beside the two-stage flow. Nothing here touches the pipeline.
"""

import pytest

pytestmark = pytest.mark.django_db


def test_every_topic_maps_to_an_uzbek_block():
    """A topic with no block silently falls back, so the map must be complete by test."""
    from apps.digest.llm import TOPIC_SHAPES, UZ_BLOCKS
    from apps.digest.models import Topic

    expected = {t.value for t in Topic} - {Topic.IRRELEVANT.value}
    assert set(TOPIC_SHAPES) == expected
    for topic, key in TOPIC_SHAPES.items():
        assert key in UZ_BLOCKS, f"{topic} maps to unknown block {key}"


def test_the_uzbek_blocks_actually_differ():
    """Seven identical blocks would pass every other test and change nothing."""
    from apps.digest.llm import UZ_BLOCKS

    assert len(UZ_BLOCKS) == 7, "six groups plus the general fallback"
    assert len({b.strip() for b in UZ_BLOCKS.values()}) == 7, "blocks must be distinct"


def test_every_uzbek_block_states_its_word_limits():
    """The block is read before the field bullets, so it has to carry the limits itself.

    Measured 2026-08-26 on the English shape blocks: the risk block produced a 24-word lead
    against an 18-word cap stated further down the prompt. The content instruction wins.
    """
    from apps.digest.llm import SHAPE_GENERAL, UZ_BLOCKS

    for key, block in UZ_BLOCKS.items():
        if key == SHAPE_GENERAL:
            assert "14 so'z" in block and "16 so'z" in block and "8 so'z" in block
            continue
        for cap in ("<= 14 so'z", "<= 16 so'z", "<= 8 so'z"):
            assert cap in block, f"{key} block does not state {cap}"


def test_no_uzbek_block_redefines_the_headline():
    """headline_uz is a label and does not change with the story type.

    The English agent block leaked into the headline on 2026-08-26 by arguing a thesis.
    Keeping headline_uz out of every block is how that stays impossible.
    """
    from apps.digest.llm import UZ_BLOCKS

    for key, block in UZ_BLOCKS.items():
        assert "headline_uz" not in block, f"{key} must not redefine the headline"
