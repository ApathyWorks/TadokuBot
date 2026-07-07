"""Tests for the Pillow profile-card renderer (lib.profile_card).

Rendering is inherently visual, so these assert the contract rather than pixels:
a valid PNG comes back, at the expected size, with or without an avatar, and the
compact count formatting is correct.
"""

import io

from PIL import Image

import lib.profile_card as profile_card


def _valid_png(data: bytes) -> Image.Image:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    return Image.open(io.BytesIO(data))


def test_format_count_is_human_readable():
    assert profile_card._format_count(6_600_000) == "6.6M"
    assert profile_card._format_count(12_300) == "12.3k"
    assert profile_card._format_count(812) == "812"
    assert profile_card._format_count(0) == "0"


async def test_render_card_returns_png_of_expected_size():
    data = await profile_card.render_card(
        display_name="strangefella",
        subtitle="Immersion profile",
        avatar_bytes=None,
        characters=6_600_000,
        pages=1234,
        listening_hours=0.0,
        this_log="Reading  ·  192 Page  ·  +192 pts",
    )
    img = _valid_png(data)
    assert img.size == (profile_card.WIDTH, profile_card.HEIGHT)


async def test_render_card_accepts_a_real_avatar_image():
    # A tiny real PNG as the avatar; the renderer should crop/mask it without error.
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="PNG")
    data = await profile_card.render_card(
        display_name="ruby",
        avatar_bytes=buf.getvalue(),
        characters=1000,
        pages=10,
        listening_hours=1.5,
        this_log="Listening  ·  90 Minute  ·  +90 pts",
    )
    assert _valid_png(data).size == (profile_card.WIDTH, profile_card.HEIGHT)


async def test_render_card_tolerates_garbage_avatar_bytes():
    # Undecodable avatar bytes -> placeholder disc, still a valid card.
    data = await profile_card.render_card(
        display_name="ruby", avatar_bytes=b"not-an-image", this_log="Reading  ·  1 Page  ·  +1 pts"
    )
    assert _valid_png(data).size == (profile_card.WIDTH, profile_card.HEIGHT)


async def test_render_card_draws_a_japanese_title_without_error():
    data = await profile_card.render_card(
        display_name="strangefella",
        this_log="Reading  ·  192 Page  ·  +192 pts",
        title="奇跡を、生きている",
    )
    assert _valid_png(data).size == (profile_card.WIDTH, profile_card.HEIGHT)


async def test_render_card_truncates_an_overlong_title():
    # A very long title must not overflow the card; it just gets ellipsized.
    data = await profile_card.render_card(
        display_name="ruby", this_log="Reading  ·  1 Page  ·  +1 pts", title="タイトル" * 60
    )
    assert _valid_png(data).size == (profile_card.WIDTH, profile_card.HEIGHT)


def test_truncate_shortens_text_that_is_too_wide():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = profile_card._font(40)
    out = profile_card._truncate(draw, "x" * 500, font, max_width=200)
    assert out.endswith("…")
    assert draw.textlength(out, font=font) <= 200
