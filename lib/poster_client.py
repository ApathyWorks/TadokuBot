"""Fetch a cover/poster image for a log's material, chosen by the log's tags.

A tadoku log carries a ``tags`` list (e.g. ``["manga"]``, ``["fiction", "game"]``)
and a ``description`` that is the material's title -- often Japanese, and often
carrying volume/episode noise like ``"呪術廻戦 Vol. 1"`` or ``"転スラ７３〜８３"``.
This module maps a log to a poster image, by tag:

  * vn          -> VNDB (``api.vndb.org/kana``), no key needed
  * game        -> VNDB, then Steam's storefront as a fallback (both keyless);
                   Steam covers general games (Persona, Ys, …) that aren't VNs
  * anime       -> AniList GraphQL (keyless), then MyAnimeList (needs ``MAL_CLIENT_ID``)
  * manga       -> AniList GraphQL (keyless), then MyAnimeList (needs ``MAL_CLIENT_ID``)
  * ln          -> AniList GraphQL (light novels, keyless), then Google Books
  * book        -> AniList GraphQL (keyless), then Google Books (needs
                   ``GOOGLE_BOOKS_API_KEY`` for quota). AniList carries Japanese
                   light-novel covers Google Books often lacks, so it's tried first
  * audiobook   -> same lookup as ``book`` (a book read aloud): AniList, then
                   Google Books
  * tv/movie/show (live-action) -> TMDB (needs ``TMDB_API_KEY``); anime is routed
                   to AniList / MyAnimeList above, so only non-anime screen media
                   lands here

A VNDB cover that VNDB itself flags as NSFW is never posted: the lookup swaps in
the bundled ``images/anime-disgust.png`` instead (see ``_vndb_is_nsfw``).

Every lookup is strictly best-effort: a miss, a missing API key, or any
network/parse failure yields ``None`` so the log feed simply falls back to the
poster-less card. The title is cleaned of volume/episode markers first, which
markedly improves the search hit-rate against every service.

Nothing here authenticates as a user or writes anywhere -- it's read-only
lookups against public search endpoints, keyed only by the material title.
"""

import asyncio
import logging
import os
import re
from typing import Optional
from urllib.parse import quote

import aiohttp

_log = logging.getLogger(__name__)

# Cap every request so a slow upstream can't stall the log-feed poll. Posters are
# a nice-to-have, so we fail fast rather than hold the card.
_TIMEOUT = aiohttp.ClientTimeout(total=8)

# The stand-in poster used in place of a VNDB cover flagged NSFW. Resolved
# relative to this file (lib/ -> project root) so it works from any cwd.
_NSFW_POSTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "anime-disgust.png"
)

# VNDB's own NSFW cutoff for its 0-2 image-flagging averages. VNDB's API marks an
# image NSFW when either the sexual or violence average is above 0.4 -- the point
# its site starts calling an image "Suggestive"/"Violent" and hides it by default.
_VNDB_NSFW_THRESHOLD = 0.4

# Sentinel returned instead of a URL when the matched cover is NSFW; tells
# ``fetch_poster`` to use the local stand-in rather than download anything.
_NSFW = "nsfw"

# Cap the search query length -- MAL rejects overly long ``q`` values, and the
# extra words past a title's head only hurt the match anyway.
_MAX_QUERY = 64

# Which service a set of tags routes to. A log can carry several tags
# (``["fiction", "game"]``); the first match here wins, most-specific first.
def _category(tags: Optional[list]) -> Optional[str]:
    """Map a log's tags to a poster source key, or ``None`` if none apply."""
    if not tags:
        return None
    have = {str(t).lower() for t in tags}
    # ``vn`` is VNDB-only; ``game`` tries VNDB then Steam. Check ``vn`` first so a
    # log tagged both keeps the (better) VNDB art without a needless Steam hop.
    if "vn" in have:
        return "vn"
    if "game" in have:
        return "game"
    if "anime" in have:
        return "anime"
    if "manga" in have:
        return "manga"
    # ``ln`` and ``book`` both try AniList first; ``ln`` narrows AniList to novels,
    # so a log tagged both prefers that (more precise) light-novel search.
    if "ln" in have:
        return "ln"
    # An audiobook is a book read aloud -- route it through the same cover lookup
    # as ``book`` (AniList, then Google Books). ``ln`` is checked first, so an
    # audiobook of a light novel still gets the more precise novel search.
    if have & {"book", "audiobook"}:
        return "book"
    # Live-action screen media: TV shows and films. ``anime`` is caught above, so
    # a log tagged both (e.g. an anime that aired on TV) stays on MyAnimeList.
    if have & {"tv", "movie", "show"}:
        return "screen"
    return None


def clean_title(description: str) -> str:
    """Strip volume/episode/range noise from a log title for searching.

    Turns e.g. ``"呪術廻戦 Vol. 1"`` -> ``"呪術廻戦"``, ``"ナルト vol. 14 (finished)"``
    -> ``"ナルト"``, ``"転スラ７３〜８３"`` -> ``"転スラ"``, ``"薫る花は凛と咲く ep 1-3"``
    -> ``"薫る花は凛と咲く"``. Best-effort: if cleaning would empty the string, the
    original (trimmed) title is returned instead.
    """
    t = (description or "").strip()
    if not t:
        return ""
    # Drop parenthetical / bracketed notes: (finished), 【...】, [...], （...）.
    t = re.sub(r"[\(（【\[].*?[\)）】\]]", " ", t)
    t = re.sub(r"[\(（【\[].*$", " ", t)  # unbalanced trailing "(finished"
    # Cut at a Latin volume/season/episode/chapter marker and everything after it.
    t = re.sub(
        r"(?i)\b(?:(?:vol\.?|volume|ep\.?|episode|chapter|ch\.?|#)\s*[\d０-９]|s[\d０-９]+e[\d０-９]+).*$",
        "",
        t,
    )
    # Cut at a CJK volume/episode counter: 第N話 / N巻 / N話 / N章 / N集 / N冊.
    t = re.sub(r"第?\s*[\d０-９]+\s*(?:話|巻|章|集|冊).*$", "", t)
    # Cut a trailing bare number or number-range (14, 002, 73〜83, 1-2).
    t = re.sub(r"[\d０-９]+(?:\s*[〜~\-–ー]\s*[\d０-９]+)?\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" 　-–—・:：")
    return t or (description or "").strip()


async def fetch_poster(
    session: aiohttp.ClientSession,
    tags: Optional[list],
    description: str,
    cache: Optional[dict] = None,
) -> Optional[bytes]:
    """Return poster image bytes for a log, or ``None`` if none can be found.

    Routes on the log's ``tags`` (see ``_category``), cleans ``description`` into
    a search query, looks up an image URL from the matching service, and
    downloads it. A cover VNDB flags as NSFW is replaced by the bundled stand-in
    image rather than downloaded. Any failure at any step -- unknown category,
    empty title, missing key, network error, decode-less bytes -- collapses to
    ``None`` so the caller can fall back to the poster-less card.

    ``cache`` (if given) memoises results by (category, title) so a burst of the
    same material costs a single lookup + download.
    """
    category = _category(tags)
    if category is None:
        return None
    title = clean_title(description)
    if not title:
        return None
    key = (category, title.lower())
    if cache is not None and key in cache:
        return cache[key]

    result: Optional[bytes] = None
    try:
        url = await _image_url(session, category, title[:_MAX_QUERY])
        if url == _NSFW:
            result = _nsfw_poster()
        elif url:
            result = await _download(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        _log.warning("Poster lookup for %r (%s) failed: %s", title, category, e)
        result = None

    if cache is not None:
        cache[key] = result
    return result


async def _image_url(
    session: aiohttp.ClientSession, category: str, title: str
) -> Optional[str]:
    """Dispatch to the right service and return a poster image URL, or ``None``."""
    if category == "vn":
        return await _vndb_image_url(session, title)
    if category == "game":
        # VNs have the nicest art on VNDB; fall back to Steam for everything else.
        # An NSFW VNDB hit is still a hit (the ``_NSFW`` sentinel is truthy): it
        # names this exact work, so we don't trade the stand-in for a Steam guess.
        return await _vndb_image_url(session, title) or await _steam_image_url(session, title)
    if category in ("anime", "manga"):
        # AniList first (keyless, strong Japanese coverage), then MyAnimeList.
        media_type = "ANIME" if category == "anime" else "MANGA"
        return await _anilist_image_url(session, title, media_type=media_type) \
            or await _mal_image_url(session, category, title)
    if category == "ln":
        # Light novels: AniList's NOVEL search, then Google Books.
        return await _anilist_image_url(session, title, novel_only=True) \
            or await _google_books_image_url(session, title)
    if category == "book":
        # Books: AniList (best manga-or-novel match), then Google Books.
        return await _anilist_image_url(session, title, novel_only=False) \
            or await _google_books_image_url(session, title)
    if category == "screen":
        return await _tmdb_image_url(session, title)
    return None


async def _mal_image_url(
    session: aiohttp.ClientSession, media: str, title: str
) -> Optional[str]:
    """Look up an anime/manga cover on MyAnimeList v2 (``main_picture``).

    Needs ``MAL_CLIENT_ID`` in the environment; without it we skip (return
    ``None``) rather than error.
    """
    client_id = os.environ.get("MAL_CLIENT_ID")
    if not client_id:
        return None
    url = f"https://api.myanimelist.net/v2/{media}"
    params = {"q": title, "limit": 1, "fields": "main_picture"}
    headers = {"X-MAL-CLIENT-ID": client_id}
    async with session.get(url, params=params, headers=headers, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    nodes = data.get("data") or []
    if not nodes:
        return None
    picture = nodes[0].get("node", {}).get("main_picture") or {}
    return picture.get("large") or picture.get("medium")


async def _tmdb_image_url(
    session: aiohttp.ClientSession, title: str
) -> Optional[str]:
    """Look up a live-action TV/movie poster on TMDB (``poster_path``).

    Uses TMDB's *multi* search, which matches a title across both films and TV in
    one call and matches Japanese titles well (TMDB indexes each work's original
    and alternative-language titles). Needs ``TMDB_API_KEY`` (a v3 API key);
    without it we skip (return ``None``) rather than error.
    """
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        return None
    params = {
        "api_key": key,
        "query": title,
        "include_adult": "false",
        # Prefer Japanese-language metadata/artwork where a work has it.
        "language": "ja-JP",
    }
    async with session.get(
        "https://api.themoviedb.org/3/search/multi", params=params, timeout=_TIMEOUT
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    for result in data.get("results") or []:
        # Skip ``person`` hits; take the first film/show that actually has a poster.
        if result.get("media_type") in ("movie", "tv") and result.get("poster_path"):
            return f"https://image.tmdb.org/t/p/w500{result['poster_path']}"
    return None


async def _vndb_image_url(
    session: aiohttp.ClientSession, title: str
) -> Optional[str]:
    """Look up a visual-novel cover on VNDB's Kana API (no key needed).

    Returns the cover URL, ``None`` on a miss, or the ``_NSFW`` sentinel when VNDB
    flags the cover NSFW (see ``_vndb_is_nsfw``).
    """
    body = {
        "filters": ["search", "=", title],
        "fields": "image.url, image.sexual, image.violence, image.votecount",
        "results": 1,
    }
    async with session.post(
        "https://api.vndb.org/kana/vn", json=body, timeout=_TIMEOUT
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    results = data.get("results") or []
    if not results:
        return None
    image = results[0].get("image") or {}
    url = image.get("url")
    if url and _vndb_is_nsfw(image):
        return _NSFW
    return url


def _vndb_is_nsfw(image: dict) -> bool:
    """Whether VNDB considers a cover NSFW, by VNDB's own rule.

    Mirrors how VNDB computes its API's ``nsfw`` flag: an image is NSFW if its
    average sexual *or* violence flagging vote is above 0.4 (on the 0-2 scale), or
    if nobody has voted on it yet -- VNDB treats an unrated image as NSFW until
    someone checks it, and so do we. A missing vote count reads as unrated, so a
    response without the flagging fields errs toward the stand-in, not the cover.
    """
    if not image.get("votecount"):
        return True
    sexual = image.get("sexual") or 0
    violence = image.get("violence") or 0
    return sexual > _VNDB_NSFW_THRESHOLD or violence > _VNDB_NSFW_THRESHOLD


def _nsfw_poster() -> Optional[bytes]:
    """The stand-in poster's bytes, or ``None`` if the file can't be read.

    Read on use rather than at import, so a missing file costs only that card its
    poster (and a warning) -- never the bot its startup, and never a fallback to
    the NSFW cover. ``fetch_poster``'s cache keeps repeats from re-reading it.
    """
    try:
        with open(_NSFW_POSTER_PATH, "rb") as f:
            return f.read()
    except OSError as e:
        _log.warning("NSFW stand-in poster unavailable at %s: %s", _NSFW_POSTER_PATH, e)
        return None


async def _steam_image_url(
    session: aiohttp.ClientSession, title: str
) -> Optional[str]:
    """Look up a game cover on Steam's storefront (no key needed).

    Searches the keyless app-search endpoint (which matches English *and*
    Japanese titles), then prefers the 2:3 portrait "library" capsule, falling
    back to the landscape header for the rare title without portrait art. Steam
    is PC-only, so console-exclusive titles simply won't match (-> ``None``).
    """
    search_url = "https://steamcommunity.com/actions/SearchApps/" + quote(title, safe="")
    async with session.get(search_url, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        results = await resp.json()
    if not results:
        return None
    appid = results[0].get("appid")
    if not appid:
        return None
    base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
    for filename in ("library_600x900.jpg", "header.jpg"):
        candidate = f"{base}/{filename}"
        # A cheap HEAD confirms the image exists before we commit to it.
        async with session.head(candidate, timeout=_TIMEOUT) as resp:
            if resp.status == 200:
                return candidate
    return None


async def _anilist_image_url(
    session: aiohttp.ClientSession, title: str, *, media_type: str = "MANGA", novel_only: bool = False
) -> Optional[str]:
    """Look up a cover on AniList's GraphQL API (no key needed).

    ``media_type`` selects the AniList type: ``"ANIME"`` (the ``anime`` tag) or
    ``"MANGA"`` -- which also covers manga, light novels and books, since AniList
    indexes novels under ``type: MANGA``. ``novel_only`` (MANGA only) narrows the
    search to ``format: NOVEL`` for the ``ln`` tag. Returns the largest
    ``coverImage`` URL, or ``None`` on a miss / error (so the caller can fall back
    to MyAnimeList / Google Books). A "not found" comes back as either a 200 with
    ``Media`` null or a 404 -- both yield ``None`` here.
    """
    fmt = ", format: NOVEL" if novel_only else ""
    query = (
        f"query ($search: String) {{ Media(search: $search, type: {media_type}{fmt}) "
        f"{{ coverImage {{ extraLarge large medium }} }} }}"
    )
    body = {"query": query, "variables": {"search": title}}
    async with session.post(
        "https://graphql.anilist.co", json=body, timeout=_TIMEOUT
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    media = (data.get("data") or {}).get("Media") or {}
    cover = media.get("coverImage") or {}
    return cover.get("extraLarge") or cover.get("large") or cover.get("medium")


async def _google_books_image_url(
    session: aiohttp.ClientSession, title: str
) -> Optional[str]:
    """Look up a book cover on Google Books (``imageLinks.thumbnail``).

    Needs ``GOOGLE_BOOKS_API_KEY`` for a usable quota; the keyless endpoint is
    shared and rate-limited, so without a key we skip.
    """
    key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if not key:
        return None
    params = {"q": title, "maxResults": 1, "country": "US", "key": key}
    async with session.get(
        "https://www.googleapis.com/books/v1/volumes", params=params, timeout=_TIMEOUT
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    items = data.get("items") or []
    if not items:
        return None
    links = items[0].get("volumeInfo", {}).get("imageLinks") or {}
    thumb = links.get("thumbnail") or links.get("smallThumbnail")
    # Google serves thumbnails over http; upgrade so the download isn't blocked.
    return thumb.replace("http://", "https://") if thumb else None


async def _download(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Download an image URL to bytes, or ``None`` on a non-200 / transport error."""
    async with session.get(url, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            return None
        return await resp.read()
