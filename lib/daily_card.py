"""Render the end-of-day "top logger" spotlight as a styled PNG card (Pillow).

Posted by the daily alert (``cogs.alerts``) for the tadoku day that just ended.
Same dark theme as the leaderboard card, with a gold accent: a hero block with
the day's top logger's Discord avatar (a placeholder disc when they aren't
claimed), their name and the points they earned that day, then every title they
logged with the points it earned, and a call-out telling everyone else to pick
up the slack. A footer line carries the contest name and how many people logged.

Everything is authored in logical pixels, drawn at ``SCALE``x and downsampled to
``ZOOM``x -- the same geometry pipeline as ``lib.leaderboard_card``, so the two
cards post at the same width. The CPU-bound render runs off the event loop via
``render``. Names and titles are often Japanese, so the font cascade (reused
from ``lib.profile_card``) prefers a CJK-capable face.
"""

import asyncio
import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageChops, ImageDraw

from lib.leaderboard_card import GOLD, MARGIN, SCALE, WIDTH, ZOOM, _wrap
from lib.profile_card import (
    BG, HAIRLINE, INK, INK_SOFT, PANEL_BG,
    _circular_avatar, _font, _oneline, _truncate,
)

# Most title rows drawn before the list collapses its tail into a "+ N more"
# row, so a marathon day can't produce an absurdly tall card.
MAX_TITLE_ROWS = 8

# Hero block (logical px): a large avatar beside the name and the day's points.
HERO_AVATAR_D = 128
HERO_GAP = 28
RING_W = 4

TITLE_ROW_H = 38


@dataclass
class DailyTopCard:
    """The data the daily top-logger card renders.

    ``titles`` is ``[(title, points), ...]`` in display order (the builder sorts
    it, most points first); past ``MAX_TITLE_ROWS`` the tail is summarised.
    ``avatar`` is the top logger's Discord avatar bytes, or ``None`` for a
    placeholder disc. ``date_label`` names the day covered (e.g. "Sunday,
    September 13, 2026"); ``note_title`` + ``note_body`` are the pick-up-the-slack
    call-out; ``footer`` carries the contest name and logger count.
    """
    name: str
    score: float
    titles: list[tuple[str, float]]
    date_label: str
    note_body: str
    footer: str
    avatar: Optional[bytes] = None
    note_title: Optional[str] = None


def _points(value: float) -> str:
    """Points as the leaderboards show them: one decimal, thousands separated."""
    return f"{value:,.1f}"


def _title_rows(titles: list[tuple[str, float]]) -> list[tuple[str, str, bool]]:
    """Rows to draw as ``(label, points_text, is_overflow)``.

    Up to ``MAX_TITLE_ROWS`` titles are shown as-is; beyond that the last slot
    becomes a soft "+ N more titles" row carrying their combined points, so the
    list never grows past ``MAX_TITLE_ROWS`` rows and no points go unaccounted.
    """
    if len(titles) <= MAX_TITLE_ROWS:
        return [(title, _points(points), False) for title, points in titles]
    shown = titles[: MAX_TITLE_ROWS - 1]
    rest = titles[MAX_TITLE_ROWS - 1:]
    rows = [(title, _points(points), False) for title, points in shown]
    label = f"+ {len(rest)} more title{'s' if len(rest) != 1 else ''}"
    rows.append((label, _points(sum(points for _, points in rest)), True))
    return rows


def _render(card: DailyTopCard) -> bytes:
    """Compose the daily top-logger card and return PNG bytes (worker thread)."""
    S = SCALE
    content_x = MARGIN
    right = WIDTH - MARGIN

    kicker_font = _font(16 * S, bold=True)
    date_font = _font(22 * S)
    name_font = _font(36 * S, bold=True)
    score_font = _font(46 * S, bold=True)
    score_label_font = _font(22 * S)
    section_font = _font(15 * S, bold=True)
    title_font = _font(22 * S)
    title_points_font = _font(22 * S, bold=True)
    note_title_font = _font(15 * S, bold=True)
    note_body_font = _font(21 * S)
    footer_font = _font(16 * S)

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # -- layout pass: stack the blocks top to bottom so the canvas height fits. --
    kicker_y = MARGIN
    date_y = kicker_y + 26
    hero_y = date_y + 50
    hero_mid = hero_y + HERO_AVATAR_D / 2
    divider_y = hero_y + HERO_AVATAR_D + 26
    section_y = divider_y + 22
    rows = _title_rows(card.titles)
    rows_y = section_y + 30
    y = rows_y + len(rows) * TITLE_ROW_H + 14

    pad = 18
    body_max = (right - content_x - 2 * pad - 8) * S
    note_lines = _wrap(measure, card.note_body, note_body_font, body_max)
    note_title_h = 26 if card.note_title else 0
    note_h = 2 * pad + note_title_h + len(note_lines) * 30
    note_y = y
    y += note_h + 14

    footer_y = y + 2
    total_h = footer_y + 22 + MARGIN

    # -- draw pass (SCALE space; downsampled to ZOOM x logical at the end). --
    img = Image.new("RGBA", (WIDTH * S, total_h * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer_box = (0, 0, WIDTH * S - 1, total_h * S - 1)
    outer_radius = 28 * S
    draw.rounded_rectangle(outer_box, radius=outer_radius, fill=BG)
    draw.rounded_rectangle((0, 0, 10 * S, total_h * S - 1), radius=10 * S, fill=GOLD)
    draw.rectangle((6 * S, 0, 12 * S, total_h * S - 1), fill=GOLD)

    # Header: a gold kicker, then the day this card covers.
    draw.text((content_x * S, kicker_y * S), "TOP LOGGER OF THE DAY",
              font=kicker_font, fill=GOLD, anchor="la")
    draw.text((content_x * S, date_y * S), _truncate(draw, _oneline(card.date_label), date_font,
              (right - content_x) * S), font=date_font, fill=INK_SOFT, anchor="la")

    # Hero: the avatar in a gold ring, then name over the day's points.
    d = HERO_AVATAR_D * S
    avatar = _circular_avatar(card.avatar, d)
    ax, ay = content_x * S, hero_y * S
    img.paste(avatar, (ax, ay), avatar)
    ring = RING_W * S
    draw.ellipse((ax - ring // 2, ay - ring // 2, ax + d + ring // 2, ay + d + ring // 2),
                 outline=GOLD, width=ring)

    text_x = (content_x + HERO_AVATAR_D + HERO_GAP) * S
    text_max = right * S - text_x
    draw.text((text_x, (hero_mid - 8) * S), _truncate(draw, _oneline(card.name), name_font, text_max),
              font=name_font, fill=INK, anchor="ls")
    score_text = _points(card.score)
    draw.text((text_x, (hero_mid + 50) * S), score_text, font=score_font, fill=GOLD, anchor="ls")
    score_w = draw.textlength(score_text, font=score_font)
    draw.text((text_x + score_w + 12 * S, (hero_mid + 50) * S), "points",
              font=score_label_font, fill=INK_SOFT, anchor="ls")

    draw.line((content_x * S, divider_y * S, right * S, divider_y * S), fill=HAIRLINE, width=S)

    # Title list: every title logged that day with the points it earned.
    draw.text((content_x * S, section_y * S), "TITLES LOGGED",
              font=section_font, fill=INK_SOFT, anchor="la")
    for i, (label, points_text, is_overflow) in enumerate(rows):
        mid = (rows_y + i * TITLE_ROW_H + TITLE_ROW_H / 2) * S
        if i:
            line_y = (rows_y + i * TITLE_ROW_H) * S
            draw.line((content_x * S, line_y, right * S, line_y), fill=HAIRLINE, width=S)
        ink = INK_SOFT if is_overflow else INK
        points_w = draw.textlength(points_text, font=title_points_font)
        draw.text((right * S, mid), points_text, font=title_points_font, fill=ink, anchor="rm")
        label_max = right * S - content_x * S - points_w - 24 * S
        draw.text((content_x * S, mid), _truncate(draw, _oneline(label), title_font, label_max),
                  font=title_font, fill=ink, anchor="lm")

    # Call-out: everyone else, pick up the slack.
    draw.rounded_rectangle((content_x * S, note_y * S, right * S, (note_y + note_h) * S),
                           radius=12 * S, fill=PANEL_BG, outline=HAIRLINE, width=S)
    draw.rounded_rectangle((content_x * S, note_y * S, (content_x + 6) * S, (note_y + note_h) * S),
                           radius=3 * S, fill=GOLD)
    tx = content_x + pad + 8
    ty = note_y + pad
    if card.note_title:
        draw.text((tx * S, ty * S), _oneline(card.note_title).upper(),
                  font=note_title_font, fill=INK_SOFT, anchor="la")
        ty += note_title_h
    for line in note_lines:
        draw.text((tx * S, ty * S), line, font=note_body_font, fill=INK, anchor="la")
        ty += 30

    draw.text((content_x * S, footer_y * S), _truncate(draw, _oneline(card.footer), footer_font,
              (right - content_x) * S), font=footer_font, fill=INK_SOFT, anchor="la")

    # Clip to the rounded silhouette, add the border, downsample to ZOOM x.
    outer_mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle(outer_box, radius=outer_radius, fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), outer_mask))
    ImageDraw.Draw(img).rounded_rectangle(outer_box, radius=outer_radius, outline=HAIRLINE, width=S)

    img = img.resize((round(WIDTH * ZOOM), round(total_h * ZOOM)), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def render(card: DailyTopCard) -> bytes:
    """Render the daily top-logger card off the event loop; returns PNG bytes."""
    return await asyncio.to_thread(_render, card)
