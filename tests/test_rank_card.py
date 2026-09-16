"""Tests for the rank-change card renderer (lib.rank_card).

Rendering real PNGs (Pillow), like the other card tests: we can't assert on
pixels, so we check the bytes decode to a valid image at the shared card width
and that awkward input -- a podium rank, a long CJK name, a missing/unreadable
avatar -- renders cleanly.
"""

import io

from PIL import Image

import lib.leaderboard_card as leaderboard_card
import lib.rank_card as rank_card


def _card(**overrides):
    fields = dict(name="ruby", rank=3, description="Passed anja", footer="2026 Round 4")
    fields.update(overrides)
    return rank_card.RankChangeCard(**fields)


def _dims(card):
    img = Image.open(io.BytesIO(rank_card._render(card)))
    assert img.format == "PNG"
    return img.size


def test_render_returns_a_valid_png_at_the_shared_card_width():
    w, h = _dims(_card())
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)
    assert h > 0


def test_renders_a_break_in_and_a_non_podium_rank():
    w, h = _dims(_card(rank=17, description="Broke into the top 20"))
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM) and h > 0


def test_renders_long_cjk_name_and_a_real_avatar():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buf, "PNG")
    card = _card(
        name="涼宮ハルヒ but with an absurdly long display name that has to be truncated",
        description="Passed anja and 4 others", avatar=buf.getvalue(),
    )
    w, _ = _dims(card)
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)


def test_unreadable_avatar_falls_back_to_the_placeholder():
    w, _ = _dims(_card(avatar=b"not an image"))
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)
