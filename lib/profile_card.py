"""Render a claimed logger's log as a styled PNG "profile card" (Pillow).

Styled after VN_Club_Bot's profile card: a light cream panel with a purple
accent stripe, a circular avatar, the member's lifetime immersion stats
(characters / pages / listening hours) in a stat row, and a callout for the log
they just made. When the cog supplies a material cover (``poster_bytes``) it is
drawn in a column on the right and the card widens to fit; without one the card
keeps its base size. It is a pure renderer -- the log-feed cog fetches the
numbers, the avatar bytes and the poster and passes them in, so nothing here
touches the network.

The card is drawn at 2x and downsampled for antialiasing, and the (CPU-bound)
render runs in a thread via ``render_card`` so it never blocks the event loop.
The material title (often Japanese) is drawn in the card's log callout, so the
font cascade prefers a CJK-capable face (Noto Sans CJK / Yu Gothic / …); without
one, CJK text falls back to tofu boxes.
"""

import asyncio
import io
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

# Everything is authored in logical pixels and multiplied by SCALE when drawn,
# then the canvas is downsampled back to logical size for crisp antialiasing.
SCALE = 2
WIDTH = 1040
HEIGHT = 430

# Outer margin used to align everything: the avatar's left edge, the content's
# right edge, and the top/bottom breathing room all key off it.
MARGIN = 40

# When a material poster is supplied it's drawn in a column to the right of the
# content, widening the card by ``POSTER_PANEL`` (which subtracts the content's
# own right margin, so the gap before the poster and the margin after it stay
# even). The left-hand layout keeps its coordinates -- only the canvas grows.
POSTER_MARGIN = 28   # breathing room above/below/right of the poster
POSTER_GAP = 32      # gap between the content's right edge and the poster
POSTER_H = HEIGHT - 2 * POSTER_MARGIN          # poster fills the card height
POSTER_W = round(POSTER_H * 2 / 3)             # portrait cover, 2:3 aspect
POSTER_PANEL = POSTER_GAP + POSTER_W + POSTER_MARGIN - MARGIN

# Palette: a dark charcoal ground with near-white ink and a purple accent, so the
# card reads comfortably (and doesn't glare) in a Discord channel.
BG = (30, 31, 38)
INK = (236, 237, 242)
INK_SOFT = (150, 152, 166)
HAIRLINE = (56, 58, 68)
PANEL_BG = (40, 42, 51)
CALLOUT_BG = (44, 46, 56)
ACCENT = (150, 128, 226)
PLACEHOLDER_BG = (58, 60, 72)

# Font file candidates, best first, per weight. CJK-capable faces come first
# (they cover Latin *and* Japanese, so material titles render) -- Noto Sans CJK on
# Linux/Docker, Yu Gothic / Meiryo / MS Gothic on Windows, Hiragino on macOS --
# then Latin-only fallbacks, then ``load_default`` so rendering never fails for
# lack of a font. (Without a CJK face a Japanese title falls back to tofu boxes.)
_FONT_FILES = {
    False: [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/YuGothR.ttc", "C:/Windows/Fonts/meiryo.ttc", "C:/Windows/Fonts/msgothic.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    True: [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/meiryob.ttc", "C:/Windows/Fonts/msgothic.ttc",
        "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a scalable font at ``size`` (already scaled), preferring a real TTF."""
    for path in _FONT_FILES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _oneline(text: str) -> str:
    """Collapse all whitespace (incl. newlines/tabs) to single spaces and strip.

    Pillow's single-line text drawing raises "can't measure length of multiline
    text" on any embedded newline, and log titles sometimes contain them, so every
    string drawn on the card is flattened to one line first.
    """
    return " ".join((text or "").split())


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Trim ``text`` (adding an ellipsis) so it fits within ``max_width`` pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_width:
        text = text[:-1]
    return text + ell


def _format_count(n: float) -> str:
    """Human-readable count: 6,600,000 -> "6.6M", 12,300 -> "12.3k", 812 -> "812"."""
    n = int(round(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def _circular_avatar(avatar_bytes: Optional[bytes], size: int) -> Image.Image:
    """Return a ``size``x``size`` RGBA circular avatar (a placeholder disc if the
    bytes are missing or unreadable)."""
    over = size * 4  # oversample the mask for a smooth edge
    mask = Image.new("L", (over, over), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, over, over), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    source: Optional[Image.Image] = None
    if avatar_bytes:
        try:
            source = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        except Exception:  # noqa: BLE001 -- any decode failure -> placeholder
            source = None
    if source is None:
        source = Image.new("RGB", (size, size), PLACEHOLDER_BG)
    else:
        # Center-crop to a square, then scale to the target size.
        w, h = source.size
        side = min(w, h)
        source = source.crop(
            ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
        ).resize((size, size), Image.LANCZOS)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(source, (0, 0), mask)
    return out


def _poster_image(poster_bytes: bytes, w: int, h: int, radius: int) -> Optional[Image.Image]:
    """Return a ``w``x``h`` RGBA poster (cover-cropped, rounded), or ``None``.

    ``None`` when the bytes can't be decoded, so the caller draws the card
    without a poster rather than failing. ``w``/``h``/``radius`` are in scaled
    (drawing) pixels.
    """
    try:
        source = Image.open(io.BytesIO(poster_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 -- any decode failure -> no poster
        return None

    # Cover-crop: scale so the image fills the box, then centre-crop the overflow.
    sw, sh = source.size
    scale = max(w / sw, h / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    source = source.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    source = source.crop((left, top, left + w, top + h))

    # Rounded-corner mask (oversampled for a smooth edge).
    over = 4
    mask = Image.new("L", (w * over, h * over), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * over - 1, h * over - 1), radius=radius * over, fill=255
    )
    mask = mask.resize((w, h), Image.LANCZOS)

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(source, (0, 0), mask)
    return out


def _draw_stat(draw: ImageDraw.ImageDraw, box, label: str, value: str) -> None:
    """Draw one stat panel (rounded card with a small label over a big value).

    Sized to fit four panels across the content row.
    """
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=14 * SCALE, fill=PANEL_BG, outline=HAIRLINE, width=SCALE)
    draw.text((x0 + 18 * SCALE, y0 + 22 * SCALE), label.upper(), font=_font(13 * SCALE, bold=True), fill=INK_SOFT)
    draw.text((x0 + 18 * SCALE, y0 + 48 * SCALE), value, font=_font(28 * SCALE, bold=True), fill=INK)


def _render(
    display_name: str,
    subtitle: str,
    avatar_bytes: Optional[bytes],
    characters: float,
    pages: float,
    comic_pages: float,
    listening_hours: float,
    this_log: str,
    title: str,
    poster_bytes: Optional[bytes],
) -> bytes:
    """Compose the card and return PNG bytes (runs on a worker thread)."""
    S = SCALE
    # Flatten every user-supplied string to one line: Pillow can't draw/measure
    # text with embedded newlines (which some log titles carry).
    display_name = _oneline(display_name)
    subtitle = _oneline(subtitle)
    this_log = _oneline(this_log)
    title = _oneline(title)
    # A decodable poster widens the card by a right-hand column; anything else
    # (no bytes, or undecodable bytes) renders the original content-only card.
    poster = (
        _poster_image(poster_bytes, POSTER_W * S, POSTER_H * S, 14 * S)
        if poster_bytes
        else None
    )
    card_w = WIDTH + (POSTER_PANEL if poster is not None else 0)
    img = Image.new("RGBA", (card_w * S, HEIGHT * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    outer_box = (0, 0, card_w * S - 1, HEIGHT * S - 1)
    outer_radius = 28 * S

    # Rounded dark ground. The border is drawn last, after every layer has been
    # clipped to this same silhouette, so the accent cannot cover it.
    draw.rounded_rectangle(outer_box, radius=outer_radius, fill=BG)
    # Purple accent stripe down the left edge.
    draw.rounded_rectangle((0, 0, 10 * S, HEIGHT * S - 1), radius=10 * S, fill=ACCENT)
    draw.rectangle((6 * S, 0, 12 * S, HEIGHT * S - 1), fill=ACCENT)

    # Avatar, vertically centred on the card.
    av_size = 210 * S
    avatar = _circular_avatar(avatar_bytes, av_size)
    av_x = MARGIN * S
    av_y = (HEIGHT * S - av_size) // 2
    img.paste(avatar, (av_x, av_y), avatar)
    # Hairline ring around it.
    draw.ellipse((av_x, av_y, av_x + av_size, av_y + av_size), outline=HAIRLINE, width=S)

    # Content column: right of the avatar, ending at the right margin. Its stack
    # (name, subtitle, stats, callout) is vertically centred to match the avatar.
    content_x = av_x + av_size + MARGIN * S
    right = (WIDTH - MARGIN) * S

    # Header: name + subtitle.
    draw.text((content_x, 44 * S), display_name, font=_font(46 * S, bold=True), fill=INK)
    if subtitle:
        draw.text((content_x, 104 * S), subtitle, font=_font(22 * S), fill=INK_SOFT)

    # Stat row: Characters | Pages | Comic pages | Listening.
    stats = [
        ("Characters", _format_count(characters)),
        ("Pages", _format_count(pages)),
        ("Comic pages", _format_count(comic_pages)),
        ("Listening", f"{listening_hours:.1f}h"),
    ]
    row_y = 160 * S
    panel_h = 108 * S
    gap = 16 * S
    panel_w = (right - content_x - 3 * gap) // 4
    for i, (label, value) in enumerate(stats):
        x0 = content_x + i * (panel_w + gap)
        _draw_stat(draw, (x0, row_y, x0 + panel_w, row_y + panel_h), label, value)

    # "This log" callout with its own accent stripe. When there's a material
    # title it sits on top (quoted) with the log line beneath; otherwise the log
    # line is centred on its own.
    call_y0 = row_y + panel_h + 18 * S
    call_h = 100 * S
    call_box = (content_x, call_y0, right, call_y0 + call_h)
    draw.rounded_rectangle(call_box, radius=12 * S, fill=CALLOUT_BG, outline=HAIRLINE, width=S)
    draw.rounded_rectangle((content_x, call_y0, content_x + 6 * S, call_y0 + call_h), radius=3 * S, fill=ACCENT)

    text_x = content_x + 22 * S
    text_w = right - text_x - 18 * S
    if title:
        title_font = _font(30 * S)
        log_font = _font(24 * S, bold=True)
        draw.text((text_x, call_y0 + 18 * S), _truncate(draw, f"「{title}」", title_font, text_w),
                  font=title_font, fill=INK)
        draw.text((text_x, call_y0 + 58 * S), _truncate(draw, this_log, log_font, text_w),
                  font=log_font, fill=INK_SOFT)
    else:
        log_font = _font(28 * S, bold=True)
        draw.text((text_x, call_y0 + 36 * S), _truncate(draw, this_log, log_font, text_w),
                  font=log_font, fill=INK)

    # Material poster in the right-hand column, with a hairline frame.
    if poster is not None:
        px0 = (WIDTH - MARGIN + POSTER_GAP) * S
        py0 = POSTER_MARGIN * S
        img.paste(poster, (px0, py0), poster)
        draw.rounded_rectangle(
            (px0, py0, px0 + POSTER_W * S - 1, py0 + POSTER_H * S - 1),
            radius=14 * S, outline=HAIRLINE, width=S,
        )

    # Clip the completed composition to one shared outer mask. In particular,
    # this removes the square caps from the accent's fill rectangle at the
    # top-left and bottom-left corners.
    outer_mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(outer_mask).rounded_rectangle(
        outer_box, radius=outer_radius, fill=255
    )
    img.putalpha(ImageChops.multiply(img.getchannel("A"), outer_mask))

    # Restore a crisp border on top of the clipped artwork.
    ImageDraw.Draw(img).rounded_rectangle(
        outer_box, radius=outer_radius, outline=HAIRLINE, width=S
    )

    # Downsample for antialiasing, flatten onto transparency-friendly RGBA PNG.
    img = img.resize((card_w, HEIGHT), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def render_card(
    *,
    display_name: str,
    subtitle: str = "",
    avatar_bytes: Optional[bytes] = None,
    characters: float = 0,
    pages: float = 0,
    comic_pages: float = 0,
    listening_hours: float = 0,
    this_log: str = "",
    title: str = "",
    poster_bytes: Optional[bytes] = None,
) -> bytes:
    """Render the profile card off the event loop; returns PNG bytes.

    When ``poster_bytes`` is supplied (and decodable) the material's cover is
    drawn in a column on the right and the card widens accordingly; otherwise the
    original content-only card is returned unchanged.
    """
    return await asyncio.to_thread(
        _render, display_name, subtitle, avatar_bytes, characters, pages, comic_pages,
        listening_hours, this_log, title, poster_bytes,
    )
