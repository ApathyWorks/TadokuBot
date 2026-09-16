"""Render a rank-change "moved up" spotlight as a styled PNG card (Pillow).

Posted by the live log feed (``cogs.log_feed``) when a fresh log vaults someone
up the contest leaderboard -- past a rival, or into the tracked top slice. Same
dark theme and geometry as the other cards, with a gold "moved up" accent: an
up-arrow kicker, then a hero row with the mover's Discord avatar (a placeholder
disc when they aren't claimed), their name, a one-line note on what they did
("Passed anja", "Broke into the top 20"), and the big new rank on the right. A
footer line carries the contest name.

Everything is authored in logical pixels, drawn at ``SCALE``x and downsampled to
``ZOOM``x -- the same pipeline as the leaderboard/daily cards, so it posts at the
same width. The CPU-bound render runs off the event loop via ``render``. Names
are often Japanese, so the font cascade (reused from ``lib.profile_card``) prefers
a CJK-capable face.
"""

import asyncio
import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageChops, ImageDraw

from lib.leaderboard_card import GOLD, MARGIN, MEDAL, SCALE, WIDTH, ZOOM
from lib.profile_card import (
    BG, HAIRLINE, INK, INK_SOFT,
    _circular_avatar, _font, _oneline, _truncate,
)

HERO_AVATAR_D = 104
HERO_GAP = 24
RING_W = 4


@dataclass
class RankChangeCard:
    """The data the rank-change card renders.

    ``name`` is the mover; ``rank`` their new position (medal-tinted for the top
    three, gold otherwise); ``description`` the one-line note ("Passed anja",
    "Broke into the top 20"); ``footer`` the contest name; ``avatar`` the mover's
    Discord avatar bytes, or ``None`` for a placeholder disc.
    """
    name: str
    rank: int
    description: str
    footer: str
    avatar: Optional[bytes] = None


def _render(card: RankChangeCard) -> bytes:
    """Compose the rank-change card and return PNG bytes (runs on a worker thread)."""
    S = SCALE
    content_x = MARGIN
    right = WIDTH - MARGIN
    rank_color = MEDAL.get(card.rank, GOLD)

    kicker_font = _font(16 * S, bold=True)
    name_font = _font(34 * S, bold=True)
    desc_font = _font(23 * S)
    rank_font = _font(64 * S, bold=True)
    footer_font = _font(16 * S)

    # -- layout pass (logical px). --
    kicker_y = MARGIN
    hero_y = kicker_y + 38
    hero_mid = hero_y + HERO_AVATAR_D / 2
    footer_y = hero_y + HERO_AVATAR_D + 24
    total_h = footer_y + 22 + MARGIN

    # -- draw pass (SCALE space; downsampled to ZOOM x logical at the end). --
    img = Image.new("RGBA", (WIDTH * S, total_h * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    outer_box = (0, 0, WIDTH * S - 1, total_h * S - 1)
    outer_radius = 28 * S
    draw.rounded_rectangle(outer_box, radius=outer_radius, fill=BG)
    draw.rounded_rectangle((0, 0, 10 * S, total_h * S - 1), radius=10 * S, fill=GOLD)
    draw.rectangle((6 * S, 0, 12 * S, total_h * S - 1), fill=GOLD)

    # Kicker: a gold up-triangle, then "MOVED UP".
    draw.polygon(
        [((content_x + 8) * S, (kicker_y + 1) * S),
         (content_x * S, (kicker_y + 15) * S),
         ((content_x + 16) * S, (kicker_y + 15) * S)],
        fill=GOLD,
    )
    draw.text(((content_x + 26) * S, kicker_y * S), "MOVED UP",
              font=kicker_font, fill=GOLD, anchor="la")

    # Hero: avatar in a gold ring.
    d = HERO_AVATAR_D * S
    avatar = _circular_avatar(card.avatar, d)
    ax, ay = content_x * S, hero_y * S
    img.paste(avatar, (ax, ay), avatar)
    ring = RING_W * S
    draw.ellipse((ax - ring // 2, ay - ring // 2, ax + d + ring // 2, ay + d + ring // 2),
                 outline=GOLD, width=ring)

    # New rank, right-aligned and vertically centred on the avatar.
    rank_text = f"#{card.rank}"
    rank_w = draw.textlength(rank_text, font=rank_font)
    draw.text((right * S, hero_mid * S), rank_text, font=rank_font, fill=rank_color, anchor="rm")

    # Name over the one-line note, trimmed to the space left before the rank.
    text_x = (content_x + HERO_AVATAR_D + HERO_GAP) * S
    text_max = right * S - text_x - rank_w - 28 * S
    draw.text((text_x, (hero_mid - 6) * S), _truncate(draw, _oneline(card.name), name_font, text_max),
              font=name_font, fill=INK, anchor="ls")
    draw.text((text_x, (hero_mid + 30) * S), _truncate(draw, _oneline(card.description), desc_font, text_max),
              font=desc_font, fill=INK_SOFT, anchor="ls")

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


async def render(card: RankChangeCard) -> bytes:
    """Render the rank-change card off the event loop; returns PNG bytes."""
    return await asyncio.to_thread(_render, card)
