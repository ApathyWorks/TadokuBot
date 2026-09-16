"""Tests for the poster/cover lookup (lib.poster_client).

The network is faked: a tiny stand-in for aiohttp's ``session.get``/``session.post``
async-context-manager protocol lets us assert the routing (tags -> service), the
title cleaning, the response parsing per service, and that everything degrades to
``None`` on a miss / missing key / error -- without touching the real APIs.
"""

from unittest.mock import AsyncMock

import pytest

import lib.poster_client as poster_client


# ---------------------------------------------------------------------------
# fake aiohttp session
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, json_data=None, body=b""):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def read(self):
        return self._body


class _FakeSession:
    """Serves queued responses; records the (method, url, params/json) of each call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def head(self, url, **kwargs):
        return self._next("HEAD", url, **kwargs)


def _vndb_hit(url, *, sexual=0.0, violence=0.0, votecount=5):
    """A VNDB search response with one cover; rated safe unless told otherwise."""
    image = {"url": url, "sexual": sexual, "violence": violence, "votecount": votecount}
    return _FakeResponse(json_data={"results": [{"image": image}]})


@pytest.fixture(autouse=True)
def _clear_keys(monkeypatch):
    # Default to no API keys so tests are explicit about enabling a source.
    monkeypatch.delenv("MAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# clean_title
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("呪術廻戦 Vol. 1", "呪術廻戦"),
    ("ワンピース vol. 002", "ワンピース"),
    ("ナルト vol. 14 (finished)", "ナルト"),
    ("MAO Vol. 02", "MAO"),
    ("転スラ７３〜８３", "転スラ"),
    ("薫る花は凛と咲く ep 1-3", "薫る花は凛と咲く"),
    ("水曜日のダウンタウン S2014E03", "水曜日のダウンタウン"),
    ("極道さんはパパで愛妻家 ", "極道さんはパパで愛妻家"),
    ("黄泉のツガイ　1〜２", "黄泉のツガイ"),
    ("Brothers Conflict 7", "Brothers Conflict"),
    ("Summer Pockets", "Summer Pockets"),
    ("Persona 3 Reload", "Persona 3 Reload"),
])
def test_clean_title_strips_volume_and_episode_noise(raw, expected):
    assert poster_client.clean_title(raw) == expected


def test_clean_title_keeps_original_when_cleaning_would_empty_it():
    # A bare volume marker shouldn't reduce to "" -- fall back to the trimmed input.
    assert poster_client.clean_title("Vol. 3") == "Vol. 3"
    assert poster_client.clean_title("   ") == ""


# ---------------------------------------------------------------------------
# category routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tags, expected", [
    (None, None),
    ([], None),
    (["book"], "book"),
    (["audiobook"], "book"),  # an audiobook is a book read aloud -> book lookup
    (["ln"], "ln"),
    (["ln", "book"], "ln"),  # ln wins -> AniList NOVEL search
    (["ln", "audiobook"], "ln"),  # ln still wins -> more precise novel search
    (["manga", "ln"], "manga"),  # manga still beats ln
    (["fiction", "game"], "game"),
    (["vn"], "vn"),
    (["vn", "game"], "vn"),  # vn wins so it stays VNDB-only, no Steam hop
    (["anime", "tv"], "anime"),  # anime beats tv -> MyAnimeList, not TMDB
    (["anime", "movie"], "anime"),
    (["comic", "manga"], "manga"),
    (["tv"], "screen"),
    (["movie"], "screen"),
    (["show"], "screen"),
    (["drama", "tv"], "screen"),  # live-action tv -> TMDB
    (["video"], None),  # generic "video" isn't routed to TMDB
    (["podcast"], None),
])
def test_category_routing(tags, expected):
    assert poster_client._category(tags) == expected


# ---------------------------------------------------------------------------
# fetch_poster dispatch + parsing
# ---------------------------------------------------------------------------

async def test_fetch_poster_returns_none_for_untagged_without_touching_network():
    session = _FakeSession([])  # no responses queued -> a network call would IndexError
    assert await poster_client.fetch_poster(session, [], "anything") is None
    assert session.calls == []


async def test_fetch_poster_returns_none_for_empty_title():
    session = _FakeSession([])
    assert await poster_client.fetch_poster(session, ["game"], "   ") is None
    assert session.calls == []


async def test_vndb_game_lookup_parses_image_url_and_downloads():
    session = _FakeSession([
        _vndb_hit("https://t.vndb.org/cv/x.jpg"),
        _FakeResponse(body=b"IMGBYTES"),
    ])
    out = await poster_client.fetch_poster(session, ["fiction", "game"], "Summer Pockets")
    assert out == b"IMGBYTES"
    # First call is the VNDB search POST with a cleaned search filter.
    method, url, kwargs = session.calls[0]
    assert method == "POST" and "api.vndb.org/kana/vn" in url
    assert kwargs["json"]["filters"] == ["search", "=", "Summer Pockets"]
    # The flagging fields are requested too, so NSFW covers can be caught.
    fields = kwargs["json"]["fields"]
    for field in ("image.url", "image.sexual", "image.violence", "image.votecount"):
        assert field in fields


async def test_vndb_returns_none_when_no_results():
    # ``vn`` is VNDB-only, so an empty result is a clean miss (no Steam fallback).
    session = _FakeSession([_FakeResponse(json_data={"results": []})])
    assert await poster_client.fetch_poster(session, ["vn"], "Nonexistent VN") is None
    assert len(session.calls) == 1  # search only; nothing else attempted


async def test_vndb_returns_none_when_image_is_null():
    session = _FakeSession([_FakeResponse(json_data={"results": [{"image": None}]})])
    assert await poster_client.fetch_poster(session, ["vn"], "Coverless VN") is None


# ---------------------------------------------------------------------------
# VNDB NSFW covers -> bundled stand-in
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rating", [
    {"sexual": 1.8},                 # explicit
    {"sexual": 0.5},                 # suggestive -- just past VNDB's 0.4 cutoff
    {"violence": 0.9},               # violent, however tame sexually
    {"votecount": 0},                # unrated -- VNDB treats these as NSFW too
], ids=["explicit", "suggestive", "violent", "unrated"])
async def test_vndb_nsfw_cover_is_replaced_by_stand_in(rating):
    # Only the search is queued: downloading the NSFW cover would IndexError.
    session = _FakeSession([_vndb_hit("https://t.vndb.org/cv/nsfw.jpg", **rating)])
    out = await poster_client.fetch_poster(session, ["vn"], "Some VN")
    with open(poster_client._NSFW_POSTER_PATH, "rb") as f:
        assert out == f.read()
    assert len(session.calls) == 1


async def test_vndb_rating_at_the_cutoff_is_still_safe():
    # VNDB's rule is strictly "above 0.4", so an average of exactly 0.4 posts the cover.
    session = _FakeSession([
        _vndb_hit("https://t.vndb.org/cv/ok.jpg", sexual=0.4, violence=0.4),
        _FakeResponse(body=b"COVER"),
    ])
    assert await poster_client.fetch_poster(session, ["vn"], "Some VN") == b"COVER"


async def test_game_with_nsfw_vndb_cover_uses_stand_in_not_steam():
    # VNDB matched the work, so its NSFW flag stands: no Steam lookup (would IndexError).
    session = _FakeSession([_vndb_hit("https://t.vndb.org/cv/nsfw.jpg", sexual=2.0)])
    out = await poster_client.fetch_poster(session, ["game"], "Some Eroge")
    assert out is not None
    assert len(session.calls) == 1


async def test_nsfw_cover_with_missing_stand_in_yields_no_poster(monkeypatch, tmp_path):
    # A missing stand-in drops the poster entirely -- it must never fall back to the NSFW cover.
    monkeypatch.setattr(poster_client, "_NSFW_POSTER_PATH", str(tmp_path / "gone.png"))
    session = _FakeSession([_vndb_hit("https://t.vndb.org/cv/nsfw.jpg", sexual=2.0)])
    assert await poster_client.fetch_poster(session, ["vn"], "Some VN") is None
    assert len(session.calls) == 1


def test_bundled_stand_in_is_a_png():
    with open(poster_client._NSFW_POSTER_PATH, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


async def test_game_falls_back_to_steam_when_vndb_misses():
    session = _FakeSession([
        _FakeResponse(json_data={"results": []}),                    # VNDB: miss
        _FakeResponse(json_data=[{"appid": "2161700", "name": "Persona 3 Reload"}]),  # Steam search
        _FakeResponse(status=200),                                   # HEAD library_600x900 exists
        _FakeResponse(body=b"STEAMCOVER"),                           # download
    ])
    out = await poster_client.fetch_poster(session, ["game"], "Persona 3 Reload")
    assert out == b"STEAMCOVER"
    # Order: VNDB POST, Steam search GET, HEAD portrait, GET download.
    assert [c[0] for c in session.calls] == ["POST", "GET", "HEAD", "GET"]
    assert "steamcommunity.com/actions/SearchApps" in session.calls[1][1]
    assert session.calls[2][1].endswith("/2161700/library_600x900.jpg")


async def test_steam_falls_back_to_header_when_no_portrait():
    session = _FakeSession([
        _FakeResponse(json_data={"results": []}),                    # VNDB miss
        _FakeResponse(json_data=[{"appid": "42", "name": "Old Game"}]),  # Steam search
        _FakeResponse(status=404),                                   # no portrait capsule
        _FakeResponse(status=200),                                   # header.jpg exists
        _FakeResponse(body=b"HEADER"),                               # download
    ])
    out = await poster_client.fetch_poster(session, ["game"], "Old Game")
    assert out == b"HEADER"
    assert session.calls[3][1].endswith("/42/header.jpg")


async def test_steam_returns_none_when_no_app_matches():
    session = _FakeSession([
        _FakeResponse(json_data={"results": []}),  # VNDB miss
        _FakeResponse(json_data=[]),               # Steam: no match either
    ])
    assert await poster_client.fetch_poster(session, ["game"], "Console Exclusive") is None


async def test_vn_tag_never_falls_back_to_steam():
    # A ``vn`` miss must not spill over to Steam (would IndexError on empty queue).
    session = _FakeSession([_FakeResponse(json_data={"results": []})])
    assert await poster_client.fetch_poster(session, ["vn"], "Obscure VN") is None
    assert len(session.calls) == 1


async def test_anime_anilist_hit_uses_anime_type_and_skips_mal():
    # ``anime`` tries AniList first (keyless); a hit means MyAnimeList is never touched.
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": {
            "coverImage": {"large": "https://al/l.jpg", "extraLarge": "https://al/xl.jpg"}}}}),
        _FakeResponse(body=b"ANIMECOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["anime"], "葬送のフリーレン 第1話")
    assert out == b"ANIMECOVER"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "https://graphql.anilist.co"
    assert "type: ANIME" in kwargs["json"]["query"]
    assert kwargs["json"]["variables"]["search"] == "葬送のフリーレン"  # cleaned of "第1話"
    assert session.calls[1][1] == "https://al/xl.jpg"  # download; no MAL call


async def test_manga_anilist_hit_uses_manga_type():
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": {"coverImage": {"large": "https://al/l.jpg"}}}}),
        _FakeResponse(body=b"MANGACOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["comic", "manga"], "呪術廻戦 Vol. 1")
    assert out == b"MANGACOVER"
    query = session.calls[0][2]["json"]["query"]
    assert "type: MANGA" in query and "format: NOVEL" not in query


async def test_anime_falls_back_to_mal_which_needs_a_client_id(monkeypatch):
    # AniList is keyless so it's always tried; MyAnimeList is skipped without a key.
    session = _FakeSession([_FakeResponse(json_data={"data": {"Media": None}})])  # AniList miss
    assert await poster_client.fetch_poster(session, ["anime"], "Frieren") is None
    assert len(session.calls) == 1
    assert session.calls[0][1] == "https://graphql.anilist.co"


async def test_manga_falls_back_to_mal_when_anilist_misses(monkeypatch):
    monkeypatch.setenv("MAL_CLIENT_ID", "test-client-id")
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": None}}),  # AniList miss
        _FakeResponse(json_data={"data": [
            {"node": {"main_picture": {"medium": "https://m/med.jpg", "large": "https://m/lrg.jpg"}}}
        ]}),
        _FakeResponse(body=b"COVER"),
    ])
    out = await poster_client.fetch_poster(session, ["comic", "manga"], "呪術廻戦 Vol. 1")
    assert out == b"COVER"
    # AniList tried first, then MyAnimeList.
    assert session.calls[0][1] == "https://graphql.anilist.co"
    method, url, kwargs = session.calls[1]
    assert method == "GET" and url.endswith("/v2/manga")
    assert kwargs["headers"]["X-MAL-CLIENT-ID"] == "test-client-id"
    assert kwargs["params"]["q"] == "呪術廻戦"  # cleaned of "Vol. 1"


async def test_mal_prefers_large_but_falls_back_to_medium(monkeypatch):
    monkeypatch.setenv("MAL_CLIENT_ID", "cid")
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": None}}),  # AniList miss -> fall back to MAL
        _FakeResponse(json_data={"data": [{"node": {"main_picture": {"medium": "https://m/med.jpg"}}}]}),
        _FakeResponse(body=b"OK"),
    ])
    assert await poster_client.fetch_poster(session, ["anime"], "X") == b"OK"
    # The download targets the medium URL (no large available).
    assert session.calls[2][1] == "https://m/med.jpg"


async def test_book_anilist_hit_parses_cover_and_downloads():
    # A ``book`` tag tries AniList first; a hit means Google Books is never touched.
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": {
            "coverImage": {"medium": "https://al/m.jpg", "large": "https://al/l.jpg",
                           "extraLarge": "https://al/xl.jpg"}}}}),
        _FakeResponse(body=b"ANILISTCOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["book"], "薬屋のひとりごと Vol. 1")
    assert out == b"ANILISTCOVER"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "https://graphql.anilist.co"
    assert kwargs["json"]["variables"]["search"] == "薬屋のひとりごと"  # cleaned of "Vol. 1"
    assert "format: NOVEL" not in kwargs["json"]["query"]  # plain book search
    assert session.calls[1][1] == "https://al/xl.jpg"  # prefers extraLarge


async def test_audiobook_uses_the_same_book_lookup():
    # An ``audiobook`` tag flows exactly like ``book``: AniList (plain search),
    # then download; Google Books is only a fallback (untouched on a hit).
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": {
            "coverImage": {"large": "https://al/l.jpg"}}}}),
        _FakeResponse(body=b"AUDIOBOOKCOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["audiobook"], "薬屋のひとりごと Vol. 1")
    assert out == b"AUDIOBOOKCOVER"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "https://graphql.anilist.co"
    assert kwargs["json"]["variables"]["search"] == "薬屋のひとりごと"  # cleaned of "Vol. 1"
    assert "format: NOVEL" not in kwargs["json"]["query"]  # plain book search


async def test_ln_anilist_search_narrows_to_novel_format():
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": {"coverImage": {"large": "https://al/l.jpg"}}}}),
        _FakeResponse(body=b"LNCOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["ln"], "オーバーロード")
    assert out == b"LNCOVER"
    assert "format: NOVEL" in session.calls[0][2]["json"]["query"]


async def test_book_falls_back_to_google_when_anilist_misses(monkeypatch):
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "gkey")
    session = _FakeSession([
        _FakeResponse(json_data={"data": {"Media": None}}),  # AniList miss
        _FakeResponse(json_data={"items": [
            {"volumeInfo": {"imageLinks": {"thumbnail": "http://books.google.com/c.jpg"}}}
        ]}),
        _FakeResponse(body=b"BOOKCOVER"),
    ])
    out = await poster_client.fetch_poster(session, ["book"], "雪国")
    assert out == b"BOOKCOVER"
    assert session.calls[0][1] == "https://graphql.anilist.co"  # AniList tried first
    assert session.calls[1][1].startswith("https://www.googleapis.com/books")
    assert session.calls[2][1] == "https://books.google.com/c.jpg"  # http -> https


async def test_book_returns_none_when_anilist_misses_and_no_google_key(monkeypatch):
    # AniList is keyless so it's always tried; Google Books is skipped without a key.
    session = _FakeSession([_FakeResponse(json_data={"data": {"Media": None}})])
    assert await poster_client.fetch_poster(session, ["book"], "雪国") is None
    assert len(session.calls) == 1
    assert session.calls[0][1] == "https://graphql.anilist.co"


async def test_tmdb_requires_key(monkeypatch):
    session = _FakeSession([])  # no key -> must not call out
    assert await poster_client.fetch_poster(session, ["tv"], "silent tokyo") is None
    assert session.calls == []


async def test_tmdb_lookup_parses_poster_and_cleans_title(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tkey")
    session = _FakeSession([
        _FakeResponse(json_data={"results": [
            {"media_type": "person", "name": "Some Actor"},          # skipped
            {"media_type": "tv", "poster_path": None},                # skipped (no poster)
            {"media_type": "movie", "poster_path": "/abc123.jpg"},    # taken
        ]}),
        _FakeResponse(body=b"TMDBPOSTER"),
    ])
    out = await poster_client.fetch_poster(session, ["movie"], "silent tokyo 1")
    assert out == b"TMDBPOSTER"
    method, url, kwargs = session.calls[0]
    assert method == "GET" and "api.themoviedb.org/3/search/multi" in url
    assert kwargs["params"]["api_key"] == "tkey"
    assert kwargs["params"]["query"] == "silent tokyo"  # cleaned of trailing "1"
    # The download targets the built image URL at w500.
    assert session.calls[1][1] == "https://image.tmdb.org/t/p/w500/abc123.jpg"


async def test_tmdb_passes_japanese_title_through(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tkey")
    session = _FakeSession([
        _FakeResponse(json_data={"results": [{"media_type": "tv", "poster_path": "/jp.jpg"}]}),
        _FakeResponse(body=b"JP"),
    ])
    out = await poster_client.fetch_poster(session, ["tv"], "半沢直樹")
    assert out == b"JP"
    assert session.calls[0][2]["params"]["query"] == "半沢直樹"


async def test_tmdb_returns_none_when_no_screen_result(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "tkey")
    session = _FakeSession([
        _FakeResponse(json_data={"results": [{"media_type": "person", "name": "X"}]}),
    ])
    assert await poster_client.fetch_poster(session, ["show"], "Nobody") is None
    assert len(session.calls) == 1  # search only; no download attempted


async def test_non_200_search_yields_none(monkeypatch):
    # ``vn`` -> VNDB only, so a 500 is a clean miss with no Steam fallback attempt.
    session = _FakeSession([_FakeResponse(status=500, json_data={})])
    assert await poster_client.fetch_poster(session, ["vn"], "Whatever") is None


async def test_non_200_download_yields_none():
    session = _FakeSession([
        _vndb_hit("https://x/y.jpg"),
        _FakeResponse(status=404),
    ])
    assert await poster_client.fetch_poster(session, ["game"], "Summer Pockets") is None


async def test_fetch_poster_memoises_via_cache():
    session = _FakeSession([
        _vndb_hit("https://x/y.jpg"),
        _FakeResponse(body=b"IMG"),
    ])
    cache: dict = {}
    a = await poster_client.fetch_poster(session, ["game"], "Summer Pockets", cache)
    # Second call for the same material must hit the cache, issuing no new requests.
    b = await poster_client.fetch_poster(session, ["game"], "Summer Pockets", cache)
    assert a == b == b"IMG"
    assert len(session.calls) == 2  # one search + one download, not four
