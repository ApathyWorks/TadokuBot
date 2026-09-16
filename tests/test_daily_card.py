"""Tests for the daily top-logger card renderer (lib.daily_card).

Rendering real PNGs (Pillow), like test_leaderboard_card.py: we can't assert on
pixels, so we check the bytes decode to a sensible image at the shared card
width, that the title list grows the card but is capped (overflow collapses into
a "+ N more" row that still accounts for every point), and that awkward input --
CJK names, over-long titles, a missing or unreadable avatar -- renders cleanly.
"""

import io

from PIL import Image

import lib.daily_card as daily_card
import lib.leaderboard_card as leaderboard_card


def _card(titles=None, **overrides):
    fields = dict(
        name="ruby",
        score=sum(points for _, points in titles) if titles else 42.0,
        titles=titles if titles is not None else [("Summer Pockets", 42.0)],
        date_label="Sunday, September 13, 2026",
        footer="2026 Round 5 · 1 person logged",
    )
    fields.update(overrides)
    return daily_card.DailyTopCard(**fields)


def _titles(n):
    return [(f"title {i}", float(100 - i)) for i in range(n)]


def _dims(card):
    img = Image.open(io.BytesIO(daily_card._render(card)))
    assert img.format == "PNG"
    return img.size  # (width, height)


def test_render_returns_a_valid_png_at_the_shared_card_width():
    w, h = _dims(_card())
    # Posts at the same width as the leaderboard cards.
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)
    assert h > 0


def test_more_titles_make_a_taller_card():
    assert _dims(_card(_titles(5)))[1] > _dims(_card(_titles(1)))[1]


def test_title_list_is_capped_so_a_marathon_day_stays_a_sane_height():
    at_cap = _dims(_card(_titles(daily_card.MAX_TITLE_ROWS)))[1]
    assert _dims(_card(_titles(daily_card.MAX_TITLE_ROWS + 1)))[1] == at_cap
    assert _dims(_card(_titles(60)))[1] == at_cap


def test_title_rows_show_everything_up_to_the_cap():
    rows = daily_card._title_rows(_titles(daily_card.MAX_TITLE_ROWS))
    assert len(rows) == daily_card.MAX_TITLE_ROWS
    assert not any(is_overflow for _, _, is_overflow in rows)
    assert rows[0] == ("title 0", "100.0", False)


def test_title_rows_collapse_the_tail_into_a_more_row_keeping_its_points():
    titles = _titles(daily_card.MAX_TITLE_ROWS + 3)
    rows = daily_card._title_rows(titles)

    assert len(rows) == daily_card.MAX_TITLE_ROWS
    label, points, is_overflow = rows[-1]
    hidden = titles[daily_card.MAX_TITLE_ROWS - 1:]
    assert is_overflow
    assert label == f"+ {len(hidden)} more titles"
    assert points == f"{sum(p for _, p in hidden):,.1f}"


def test_points_format_matches_the_leaderboards():
    assert daily_card._points(1284.5) == "1,284.5"
    assert daily_card._points(7) == "7.0"


def test_renders_cjk_long_titles_and_a_real_avatar_without_error():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 90)).save(buf, "PNG")
    card = _card(
        titles=[("呪術廻戦", 412.0), ("A light novel title far too long to fit on one row of the card " * 3, 5.0)],
        name="涼宮ハルヒ but with an absurdly long display name that has to be truncated",
        avatar=buf.getvalue(),
    )
    w, h = _dims(card)
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM) and h > 0


def test_unreadable_avatar_bytes_fall_back_to_the_placeholder():
    w, _ = _dims(_card(avatar=b"not an image"))
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)
