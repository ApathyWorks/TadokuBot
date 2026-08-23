"""Tests for the leaderboard-card renderer (lib.leaderboard_card).

Rendering real PNGs (Pillow), like test_profile_card.py: we can't assert on
pixels, so we check the bytes decode to a sensible image, that the layout grows
with the number of rows / the note block, and that the top-three rows are drawn
taller than the rest (the whole point of the card).
"""

import io

from PIL import Image

import lib.leaderboard_card as leaderboard_card


def _entries(n):
    return [{"rank": i, "name": f"user{i}", "score": 100.0 - i, "is_tie": False}
            for i in range(1, n + 1)]


def _dims(card):
    img = Image.open(io.BytesIO(leaderboard_card._render(card)))
    assert img.format == "PNG"
    return img.size  # (width, height)


def test_render_returns_a_valid_png_at_the_zoomed_width():
    card = leaderboard_card.LeaderboardCard(
        title="2026 Round 4 — last 7 days", entries=_entries(5), footer="Top 5 of 5")
    w, h = _dims(card)
    # The card renders 50% larger than the logical design.
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)
    assert h > 0


def test_renders_entries_with_avatars_without_error():
    import io as _io

    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (32, 32), (200, 100, 100)).save(buf, "PNG")
    avatar = buf.getvalue()
    card = leaderboard_card.LeaderboardCard(
        title="t",
        entries=[{"rank": 1, "name": "ruby", "score": 9.0, "is_tie": False, "avatar": avatar},
                 {"rank": 2, "name": "unclaimed", "score": 8.0, "is_tie": False}],  # no avatar key
        footer="f")
    w, _ = _dims(card)
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM)


def test_more_rows_make_a_taller_card():
    short = leaderboard_card.LeaderboardCard(title="t", entries=_entries(3), footer="f")
    tall = leaderboard_card.LeaderboardCard(title="t", entries=_entries(10), footer="f")
    assert _dims(tall)[1] > _dims(short)[1]


def test_top_three_rows_are_taller_than_the_rest():
    # Two entries (both top-three) vs. two entries pushed below the podium: the
    # all-top card must be taller, proving rank<=3 rows use the bigger row height.
    top = leaderboard_card.LeaderboardCard(
        title="t",
        entries=[{"rank": 1, "name": "a", "score": 9.0, "is_tie": False},
                 {"rank": 2, "name": "b", "score": 8.0, "is_tie": False}],
        footer="f")
    below = leaderboard_card.LeaderboardCard(
        title="t",
        entries=[{"rank": 4, "name": "a", "score": 9.0, "is_tie": False},
                 {"rank": 5, "name": "b", "score": 8.0, "is_tie": False}],
        footer="f")
    assert _dims(top)[1] > _dims(below)[1]


def test_note_block_adds_height():
    plain = leaderboard_card.LeaderboardCard(title="t", entries=_entries(3), footer="f")
    noted = leaderboard_card.LeaderboardCard(
        title="t", entries=_entries(3), footer="f",
        note_title="Shame — logged nothing", note_body="a, b, c, and many more names here")
    assert _dims(noted)[1] > _dims(plain)[1]


def test_renders_with_ties_and_cjk_names_without_error():
    card = leaderboard_card.LeaderboardCard(
        title="コンテスト — 最終順位",
        entries=[
            {"rank": 1, "name": "涼宮ハルヒ", "score": 320.5, "is_tie": True},
            {"rank": 1, "name": "長門有希", "score": 320.5, "is_tie": True},
            {"rank": 3, "name": "a really long display name that must be truncated to fit",
             "score": 12.0, "is_tie": False},
        ],
        footer="2 participants", accent="gold")
    w, h = _dims(card)
    assert w == round(leaderboard_card.WIDTH * leaderboard_card.ZOOM) and h > 0
