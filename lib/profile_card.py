"""Render a claimed logger's log as a styled PNG "profile card" (Pillow).

Styled after VN_Club_Bot's profile card: a light cream panel with a purple
accent stripe, a circular avatar, the member's lifetime immersion stats
(characters / pages / listening hours) in a stat row, and a callout for the log
they just made. It is a pure renderer -- the log-feed cog fetches the numbers and
the avatar bytes and passes them in, so nothing here touches the network.

The card is drawn at 2x and downsampled for antialiasing, and the (CPU-bound)
render runs in a thread via ``render_card`` so it never blocks the event loop.
Text is ASCII in practice (stats, English activity/unit names, romaji handles);
CJK material titles ride along in the Discord message text, not on the image, so
a CJK font isn't required.
"""

import asyncio
import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Everything is authored in logical pixels and multiplied by SCALE when drawn,
# then the canvas is downsampled back to logical size for crisp antialiasing.
SCALE = 2
WIDTH = 960
HEIGHT = 360

# Palette (from the reference card): warm cream ground, dark ink, purple accent.
BG = (251, 248, 241)
INK = (28, 27, 42)
INK_SOFT = (88, 84, 110)
HAIRLINE = (216, 210, 196)
PANEL_BG = (243, 238, 224)
CALLOUT_BG = (247, 242, 230)
ACCENT = (88, 70, 150)
PLACEHOLDER_BG = (232, 226, 212)

# Font file candidates, best first, per weight. Bundled/system DejaVu (Linux, and
# the Docker image installs it) or Arial/Segoe (Windows); ``load_default`` is the
# guaranteed last resort so rendering never fails for lack of a font.
_FONT_FILES = {
    False: ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf"],
    True: ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a scalable font at ``size`` (already scaled), preferring a real TTF."""
    for path in _FONT_FILES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


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


def _draw_stat(draw: ImageDraw.ImageDraw, box, label: str, value: str) -> None:
    """Draw one stat panel (rounded card with a small label over a big value)."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=12 * SCALE, fill=PANEL_BG, outline=HAIRLINE, width=SCALE)
    draw.text((x0 + 16 * SCALE, y0 + 12 * SCALE), label.upper(), font=_font(13 * SCALE, bold=True), fill=INK_SOFT)
    draw.text((x0 + 16 * SCALE, y0 + 34 * SCALE), value, font=_font(28 * SCALE, bold=True), fill=INK)


def _render(
    display_name: str,
    subtitle: str,
    avatar_bytes: Optional[bytes],
    characters: float,
    pages: float,
    listening_hours: float,
    this_log: str,
) -> bytes:
    """Compose the card and return PNG bytes (runs on a worker thread)."""
    S = SCALE
    img = Image.new("RGBA", (WIDTH * S, HEIGHT * S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded cream ground with a hairline border; corners left transparent.
    draw.rounded_rectangle(
        (0, 0, WIDTH * S - 1, HEIGHT * S - 1), radius=24 * S, fill=BG, outline=HAIRLINE, width=S
    )
    # Purple accent stripe down the left edge.
    draw.rounded_rectangle((0, 0, 10 * S, HEIGHT * S - 1), radius=10 * S, fill=ACCENT)
    draw.rectangle((6 * S, 0, 12 * S, HEIGHT * S - 1), fill=ACCENT)

    # Avatar.
    av_size = 200 * S
    avatar = _circular_avatar(avatar_bytes, av_size)
    av_x, av_y = 40 * S, 80 * S
    img.paste(avatar, (av_x, av_y), avatar)
    # Hairline ring around it.
    draw.ellipse((av_x, av_y, av_x + av_size, av_y + av_size), outline=HAIRLINE, width=S)

    # Header: name + subtitle.
    content_x = av_x + av_size + 36 * S
    draw.text((content_x, 74 * S), display_name, font=_font(40 * S, bold=True), fill=INK)
    if subtitle:
        draw.text((content_x, 126 * S), subtitle, font=_font(20 * S), fill=INK_SOFT)

    # Stat row: Characters | Pages | Listening.
    stats = [
        ("Characters", _format_count(characters)),
        ("Pages", _format_count(pages)),
        ("Listening", f"{listening_hours:.1f}h"),
    ]
    row_y = 176 * S
    panel_h = 96 * S
    gap = 16 * S
    right = WIDTH * S - 40 * S
    panel_w = (right - content_x - 2 * gap) // 3
    for i, (label, value) in enumerate(stats):
        x0 = content_x + i * (panel_w + gap)
        _draw_stat(draw, (x0, row_y, x0 + panel_w, row_y + panel_h), label, value)

    # "This log" callout with its own accent stripe.
    call_y0 = row_y + panel_h + 16 * S
    call_box = (content_x, call_y0, right, call_y0 + 56 * S)
    draw.rounded_rectangle(call_box, radius=10 * S, fill=CALLOUT_BG, outline=HAIRLINE, width=S)
    draw.rounded_rectangle((content_x, call_y0, content_x + 6 * S, call_y0 + 56 * S), radius=3 * S, fill=ACCENT)
    draw.text(
        (content_x + 20 * S, call_y0 + 17 * S),
        this_log,
        font=_font(20 * S, bold=True),
        fill=INK,
    )

    # Downsample for antialiasing, flatten onto transparency-friendly RGBA PNG.
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
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
    listening_hours: float = 0,
    this_log: str = "",
) -> bytes:
    """Render the profile card off the event loop; returns PNG bytes."""
    return await asyncio.to_thread(
        _render, display_name, subtitle, avatar_bytes, characters, pages, listening_hours, this_log
    )
