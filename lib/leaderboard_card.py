"""Render a contest leaderboard as a styled PNG "leaderboard card" (Pillow).

Shared by the weekly/monthly ranking commands (and their scheduled alerts) and
the year-end recap. Same dark theme as the profile card: a rounded charcoal panel
with a left accent stripe -- purple for the rolling/period rankings, gold for the
year-end final standings. The **top three finishers are drawn larger**, each with
a medal-coloured rank badge, so the podium reads at a glance; everyone below
shares a smaller, uniform row. An optional note block under the list carries the
extra prose the embeds used to (the weekly "shame" call-out, the year-end
congratulations), and a footer line carries the count/window metadata.

Like the profile card, everything is authored in logical pixels, drawn at 2x and
downsampled for antialiasing, and the CPU-bound render runs off the event loop via
``render``. Display names are often Japanese, so the font cascade (reused from
``lib.profile_card``) prefers a CJK-capable face.
"""

import asyncio
import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageChops, ImageDraw

from lib.profile_card import (
    ACCENT, BG, HAIRLINE, INK, INK_SOFT, PANEL_BG, SCALE, _font, _oneline, _truncate,
)

WIDTH = 760
MARGIN = 40

# Medal fill per top-three rank (gold / silver / bronze) for the rank badge, plus
# a gold accent stripe for the year-end card.
MEDAL = {1: (214, 175, 84), 2: (183, 189, 201), 3: (200, 143, 92)}
GOLD = (214, 175, 84)

# Row geometry (logical px). The top three get the taller row + bigger type.
TOP_ROW_H = 58
ROW_H = 42
ROW_GAP = 4


@dataclass
class LeaderboardCard:
    """The data a leaderboard card renders -- all the info the old embed carried.

    ``entries`` are dicts of ``rank``/``name``/``score``/``is_tie``. ``note_title``
    + ``note_body`` are an optional call-out block (the weekly shame list, or the
    year-end congratulations). ``accent`` picks the left-stripe colour: ``"purple"``
    (period rankings) or ``"gold"`` (year-end final standings).
    """
    title: str
    entries: list[dict]
    footer: str
    note_title: Optional[str] = None
    note_body: Optional[str] = None
    accent: str = "purple"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> list[str]:
    """Greedy word-wrap ``text`` to ``max_width`` pixels (whitespace break points)."""
    words = _oneline(text).split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        if draw.textlength(f"{current} {word}", font=font) <= max_width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _render(card: LeaderboardCard) -> bytes:
    """Compose the leaderboard card and return PNG bytes (runs on a worker thread)."""
    S = SCALE
    accent = GOLD if card.accent == "gold" else ACCENT
    content_x = MARGIN
    right = WIDTH - MARGIN

    # Fonts (scaled). Top-three rows use the bigger name/score type.
    title_font = _font(34 * S, bold=True)
    top_name_font = _font(29 * S, bold=True)
    top_score_font = _font(29 * S, bold=True)
    row_name_font = _font(23 * S)
    row_score_font = _font(23 * S, bold=True)
    top_badge_font = _font(24 * S, bold=True)
    row_badge_font = _font(19 * S, bold=True)
    tie_font = _font(16 * S)
    note_title_font = _font(15 * S, bold=True)
    note_body_font = _font(20 * S)
    footer_font = _font(16 * S)

    # A throwaway canvas just for measuring text during the layout pass.
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # -- layout pass: place every row, then the note box, then the footer, so the
    # canvas height can grow to fit exactly. --
    y = MARGIN + 46  # below the title + a little breathing room for the divider
    rows = []  # (entry, y0, row_h, is_top)
    for entry in card.entries:
        is_top = entry["rank"] <= 3
        row_h = TOP_ROW_H if is_top else ROW_H
        rows.append((entry, y, row_h, is_top))
        y += row_h + ROW_GAP

    y += 8  # gap under the list

    note_lines = None
    note_box = None
    if card.note_body:
        pad = 16
        body_max = (right - content_x - 2 * pad) * S
        note_lines = _wrap(measure, card.note_body, note_body_font, body_max)
        title_h = 24 if card.note_title else 0
        body_h = len(note_lines) * 26
        box_h = 2 * pad + title_h + body_h
        note_box = (content_x, y, box_h, pad, title_h)
        y += box_h + 10

    footer_y = y + 4
    total_h = footer_y + 22 + MARGIN

    # -- draw pass. --
    img = Image.new("RGBA", (WIDTH * S, total_h * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer_box = (0, 0, WIDTH * S - 1, total_h * S - 1)
    outer_radius = 28 * S
    draw.rounded_rectangle(outer_box, radius=outer_radius, fill=BG)
    # Accent stripe down the left edge (clipped square-cap fixed by the outer mask).
    draw.rounded_rectangle((0, 0, 10 * S, total_h * S - 1), radius=10 * S, fill=accent)
    draw.rectangle((6 * S, 0, 12 * S, total_h * S - 1), fill=accent)

    # Title + a hairline divider beneath it.
    draw.text((content_x * S, MARGIN * S), _truncate(draw, _oneline(card.title), title_font,
              (right - content_x) * S), font=title_font, fill=INK, anchor="la")
    draw.line((content_x * S, (MARGIN + 40) * S, right * S, (MARGIN + 40) * S), fill=HAIRLINE, width=S)

    badge_w = 60  # rank-badge column width
    name_x = content_x + badge_w + 16
    for entry, y0, row_h, is_top in rows:
        mid = (y0 + row_h / 2) * S
        name_font = top_name_font if is_top else row_name_font
        score_font = top_score_font if is_top else row_score_font

        # Rank badge: a medal-coloured disc with the number for the top three,
        # a plain soft "#N" for everyone else.
        if is_top:
            r = 21 * S
            cx = (content_x + badge_w / 2) * S
            draw.ellipse((cx - r, mid - r, cx + r, mid + r), fill=MEDAL[entry["rank"]])
            draw.text((cx, mid), str(entry["rank"]), font=top_badge_font, fill=BG, anchor="mm")
        else:
            draw.text(((content_x + badge_w) * S, mid), f"#{entry['rank']}",
                      font=row_badge_font, fill=INK_SOFT, anchor="rm")

        # Score (right-aligned), with a soft "(tie)" marker to its left when shared.
        score_text = f"{entry['score']:.1f}"
        score_w = draw.textlength(score_text, font=score_font)
        draw.text((right * S, mid), score_text, font=score_font, fill=INK, anchor="rm")
        score_reserve = score_w
        if entry.get("is_tie"):
            tie_w = draw.textlength("(tie)", font=tie_font)
            draw.text((right * S - score_w - 12 * S, mid), "(tie)",
                      font=tie_font, fill=INK_SOFT, anchor="rm")
            score_reserve += 12 * S + tie_w

        # Name, trimmed to the space left between the badge and the score.
        name_max = right * S - name_x * S - score_reserve - 20 * S
        draw.text((name_x * S, mid), _truncate(draw, _oneline(entry["name"]), name_font, name_max),
                  font=name_font, fill=INK, anchor="lm")

    # Note call-out (shame list / congratulations).
    if note_box is not None:
        bx, by, box_h, pad, title_h = note_box
        draw.rounded_rectangle((bx * S, by * S, right * S, (by + box_h) * S),
                               radius=12 * S, fill=PANEL_BG, outline=HAIRLINE, width=S)
        draw.rounded_rectangle((bx * S, by * S, (bx + 6) * S, (by + box_h) * S),
                               radius=3 * S, fill=accent)
        ty = by + pad
        tx = bx + pad + 8
        if card.note_title:
            draw.text((tx * S, ty * S), _oneline(card.note_title).upper(),
                      font=note_title_font, fill=INK_SOFT, anchor="la")
            ty += title_h
        for line in note_lines:
            draw.text((tx * S, ty * S), line, font=note_body_font, fill=INK, anchor="la")
            ty += 26

    # Footer metadata (count / window).
    draw.text((content_x * S, footer_y * S), _truncate(draw, _oneline(card.footer), footer_font,
              (right - content_x) * S), font=footer_font, fill=INK_SOFT, anchor="la")

    # Clip to the rounded silhouette (removes the accent's square caps), then a
    # crisp border on top, then downsample.
    outer_mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle(outer_box, radius=outer_radius, fill=255)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), outer_mask))
    ImageDraw.Draw(img).rounded_rectangle(outer_box, radius=outer_radius, outline=HAIRLINE, width=S)

    img = img.resize((WIDTH, total_h), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def render(card: LeaderboardCard) -> bytes:
    """Render the leaderboard card off the event loop; returns PNG bytes."""
    return await asyncio.to_thread(_render, card)
