"""
Mud Music Manager - Amazon Music Reader (v0.16.54, Checklist item 42d)

Reads Amazon Music playlists (Amazon's own and users' shared ones) and
albums without a login. Amazon Music's songs can't be downloaded (DRM),
so -- Michael's choice, 2026-10-09 -- it works like Spotify: the
playlist is read here and each song is found on YouTube.

Checked live, 2026-10-09: music.amazon.com's own pages are an empty shell
filled by scripts, and its player's API needs tokens only the player
gets. But Amazon's public **embed widget** (music.amazon.<site>/embed/<id>,
what "Share -> Embed" gives) is a plain page listing every song of a
playlist, user playlist or album -- title, artist and Amazon's song ID
(ASIN), in order, with the cover and (for an album) its artist. Its own
song count always matched the songs listed (up to 100 checked). An
artist's embed lists only top songs, not albums, so artists aren't
supported (Amazon publishes no discography).

Amazon gives **no song lengths**, which the YouTube search checks (±5 s,
§3.4). So each song that needs downloading is looked up by title and
artist in public catalogues (Michael's choice: look the length up, strict
match when there is none):
  1. Deezer's public search API (api.deezer.com; no key; 4 searches at a
     time) -- measured 97 of 100 chart songs, 14 of 14 album songs, 48 of
     50 user-playlist songs; the year comes from the song's Deezer album
     (one more call per album);
  2. for the songs Deezer doesn't have, Apple's iTunes search. Apple was
     the first choice, but it refuses requests after about 30 searches a
     minute (measured: HTTP 403 after ~35), so it can only be the
     second source;
  3. found by either, the song gets its length, album, year and cover,
     and the normal tiered search runs. Found by neither, it is marked
     match_without_length and only a strict official-audio match with the
     same title and artist is accepted.
A lookup that fails (connection, a catalogue's limit) leaves the song
without a length and unmarked, so it fails as "Track info missing" for
this sync and is tried again next time.

Songs are spotify_reader.Track objects whose spotify_id holds the
identity "amz:<song ASIN>", so the Spotify path handles them unchanged.
IDs keep the Amazon site of their link: "music.amazon.com/B0868WH5XX"
(playlists, user playlists and albums alike; the entry's kind says which).
"""

import html
import re
import time
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Tuple

import logger
from spotify_reader import AlbumInfo, Track

IDENTITY_PREFIX = "amz:"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
# Amazon site -> Apple store country, for the length lookup.
_COUNTRIES = {"com": "us", "co.uk": "gb", "de": "de", "fr": "fr", "it": "it", "es": "es", "ca": "ca",
              "co.jp": "jp", "com.au": "au", "com.mx": "mx", "com.br": "br", "in": "in"}
_lookup_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}


def identity(asin: str) -> str:
    return f"{IDENTITY_PREFIX}{asin}"


def _split(source_id: str) -> Tuple[str, str]:
    """("music.amazon.com", "B0868WH5XX") of a source ID."""
    host, _, item = (source_id or "").rpartition("/")
    return host or "music.amazon.com", item


def embed_link(source_id: str) -> str:
    host, item = _split(source_id)
    return f"https://{host}/embed/{item}"


def page_link(source_id: str, kind: str = "playlist") -> str:
    host, item = _split(source_id)
    if kind == "album":
        return f"https://{host}/albums/{item}"
    return f"https://{host}/{'playlists' if re.fullmatch(r'B[0-9A-Z]{9}', item) else 'user-playlists'}/{item}"


@dataclass
class AmazonMusicAlbumInfo(AlbumInfo):
    @property
    def link(self) -> str:
        return page_link(self.album_id, "album")


def _clean_title(title: str) -> str:
    """Amazon's "Song [Explicit]" -> "Song" (the tag; it isn't part of the name)."""
    return re.sub(r"\s*\[(Explicit|Clean)\]\s*$", "", title or "", flags=re.IGNORECASE).strip()


def _core(text: str) -> str:
    """For comparing titles: no "(feat. ...)" / "[with ...]" parts, lower
    case, letters and digits only."""
    text = re.sub(r"[\(\[](feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", "", text or "", flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return text.strip()


def _first_artist(artist: str) -> str:
    return re.split(r"\s*(?:,|&| feat\.? | ft\.? | x | and )\s*", artist or "", maxsplit=1, flags=re.IGNORECASE)[0]


# ---------------------------------------------------------------------------
# The embed page
# ---------------------------------------------------------------------------

@dataclass
class _Embed:
    kind: str                 # album, playlist, user-playlist, artist
    title: str
    subtitle: str             # an album's artist, a playlist's curator / "N songs"
    cover_url: Optional[str]
    songs: List[Tuple[str, str, str]]   # (ASIN, title, artist) in order


def _read_embed(source_id: str) -> _Embed:
    import requests
    response = requests.get(embed_link(source_id), headers=_HEADERS, timeout=20)
    if response.status_code == 404:
        raise ValueError("Amazon Music has no shareable page for it (is the link right, and the playlist public?)")
    response.raise_for_status()
    text = response.text
    kind = re.search(r'id="TYPE"[^>]*value="([^"]+)"', text)
    title = re.search(r'id="contentTitle"[^>]*>(.*?)</a>', text, re.S)
    sub = re.search(r'class="headerMetaData[^"]*"[^>]*>(.*?)</div>', text, re.S)
    cover = re.search(r'aria-label="Cover Art">\s*<img src="([^"]+)"', text)
    songs = []
    for item in re.findall(r'<li class="trackItem.*?</li>', text, re.S):
        asin = re.search(r'data-asin="([^"]+)"', item)
        name = re.search(r'aria-label="song, ([^"]*)"', item)
        artist = re.search(r'aria-label="artist, ([^"]*)"', item)
        if asin and name:
            songs.append((asin.group(1), html.unescape(name.group(1)).strip(),
                          html.unescape(artist.group(1)).strip() if artist else ""))
    cover_url = cover.group(1) if cover and "noImg" not in cover.group(1) else None
    if cover_url:
        cover_url = re.sub(r"\._[A-Z]{2}\d+_\.", "._SY600_.", cover_url)  # 600 px instead of 240
    return _Embed(
        kind=kind.group(1) if kind else "",
        title=html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else "",
        subtitle=html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", sub.group(1)))).strip() if sub else "",
        cover_url=cover_url,
        songs=songs,
    )


def _embed_or_error(source_id: str, what: str) -> _Embed:
    try:
        embed = _read_embed(source_id)
        if embed.kind == "artist":
            raise ValueError("that's an artist, not a playlist or album")
        if not embed.songs:
            raise ValueError("it lists no songs")
        return embed
    except Exception as e:
        logger.log_error(f"Failed to read Amazon Music {what} {source_id}: {e}")
        raise RuntimeError(
            f"Could not read Amazon Music {what} '{source_id}'. Double check the link is right and the "
            f"playlist is public. ({e})"
        ) from e


# ---------------------------------------------------------------------------
# Song lengths from Apple's catalogue
# ---------------------------------------------------------------------------

class _LookupUnavailable(Exception):
    """Apple's search can't be used right now (connection, rate limit)."""


def _apple_lookup(country: str, title: str, artist: str) -> Optional[dict]:
    """Apple's best match for this song (same title, same first artist),
    or None when Apple doesn't have it. Raises _LookupUnavailable when the
    search itself fails (after one retry)."""
    key = (country, _core(title), _core(_first_artist(artist)))
    if key in _lookup_cache:
        return _lookup_cache[key]
    import requests
    bare_title = re.sub(r"[\(\[](feat|ft|with)[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE)
    term = f"{bare_title} {_first_artist(artist)}"
    for attempt in (1, 2):
        try:
            r = requests.get("https://itunes.apple.com/search", params={
                "term": term.strip(), "entity": "song", "limit": 15, "country": country}, timeout=15)
            if r.status_code in (403, 429, 503):
                raise _LookupUnavailable(f"Apple's search answered HTTP {r.status_code}")
            r.raise_for_status()
            results = r.json().get("results") or []
            break
        except Exception as e:
            if attempt == 2:
                raise _LookupUnavailable(str(e)) from e
            time.sleep(3)
    want_title, want_artist = _core(title), _core(_first_artist(artist))

    def fits(x):
        t = _core(x.get("trackName") or "")
        a = _core(x.get("artistName") or "")
        return (t == want_title or (len(want_title) > 3 and (t.startswith(want_title) or want_title.startswith(t))
                                    and t)) and want_artist and (want_artist in a or a in want_artist)
    hit = next((x for x in results if fits(x) and _core(x.get("trackName") or "") == want_title), None) \
        or next((x for x in results if fits(x)), None)
    _lookup_cache[key] = hit
    return hit


_deezer_cache: Dict[Tuple[str, str], Optional[dict]] = {}
_deezer_years: Dict[int, Optional[str]] = {}


def _deezer_get(path: str, **params) -> dict:
    """A Deezer API call, retried once after a pause; raises
    _LookupUnavailable when it can't be used (connection, quota)."""
    import requests
    for attempt in (1, 2):
        try:
            data = requests.get(f"https://api.deezer.com/{path}", params=params, timeout=15).json()
            if isinstance(data, dict) and data.get("error"):
                raise ValueError(data["error"].get("message") or data["error"])
            return data
        except Exception as e:
            if attempt == 2:
                raise _LookupUnavailable(f"Deezer: {e}") from e
            time.sleep(3)


def _deezer_lookup(title: str, artist: str) -> Optional[dict]:
    """Deezer's match for this song (same title, same first artist) or None."""
    key = (_core(title), _core(_first_artist(artist)))
    if key in _deezer_cache:
        return _deezer_cache[key]
    bare_title = re.sub(r"[\(\[](feat|ft|with)[^\)\]]*[\)\]]", "", title, flags=re.IGNORECASE).strip()
    data = _deezer_get("search", q=f"{bare_title} {_first_artist(artist)}", limit=15)
    want_title, want_artist = _core(bare_title), key[1]

    def fits(x):
        t, a = _core(x.get("title") or ""), _core((x.get("artist") or {}).get("name") or "")
        return (t and (t == want_title or t.startswith(want_title) or want_title.startswith(t))
                and want_artist and (want_artist in a or a in want_artist))
    results = data.get("data") or []
    hit = next((x for x in results if fits(x) and _core(x.get("title") or "") == want_title), None) \
        or next((x for x in results if fits(x)), None)
    _deezer_cache[key] = hit
    return hit


def _deezer_year(album_id) -> Optional[str]:
    if album_id in _deezer_years:
        return _deezer_years[album_id]
    try:
        date = _deezer_get(f"album/{album_id}").get("release_date") or ""
    except _LookupUnavailable:
        return None
    _deezer_years[album_id] = date[:4] if date[:4].isdigit() else None
    return _deezer_years[album_id]


def _fill_lengths(source_id: str, tracks: List[Track], wanted: List[Track], album: bool) -> None:
    """Look the wanted songs up: Deezer, then Apple for Deezer's misses (see
    the module docstring)."""
    from concurrent.futures import ThreadPoolExecutor
    host, _item = _split(source_id)
    country = _COUNTRIES.get(host.split("music.amazon.", 1)[-1], "us")

    def deezer(t):
        try:
            return t, _deezer_lookup(t.name, t.primary_artist), None
        except _LookupUnavailable as e:
            return t, None, e

    with ThreadPoolExecutor(max_workers=4) as pool:
        deezer_results = list(pool.map(deezer, wanted))
    failed = [t for t, _hit, err in deezer_results if err]
    if failed:
        logger.log_error(f"Amazon Music {source_id}: Deezer couldn't be searched for {len(failed)} song(s) "
                         f"({next(err for _t, _h, err in deezer_results if err)}); they have no length this sync "
                         f"and will be tried again.")
    hits = {id(t): hit for t, hit, err in deezer_results if hit}
    album_ids = list({h["album"]["id"] for h in hits.values() if (h.get("album") or {}).get("id")})
    with ThreadPoolExecutor(max_workers=4) as pool:
        years = dict(zip(album_ids, pool.map(_deezer_year, album_ids)))
    for t in wanted:
        hit = hits.get(id(t))
        if not hit:
            continue
        t.duration_ms = int(hit.get("duration") or 0) * 1000
        alb = hit.get("album") or {}
        if not album:
            t.album_name = alb.get("title") or t.album_name
        t.year = t.year or years.get(alb.get("id"))
        if not t.cover_url:
            t.cover_url = alb.get("cover_xl") or alb.get("cover_big")

    apple_off = False
    for t, hit, err in deezer_results:
        if hit or err:
            continue
        apple = None
        if not apple_off:
            try:
                apple = _apple_lookup(country, t.name, t.primary_artist)
            except _LookupUnavailable as e:
                apple_off = True
                logger.log_error(f"Amazon Music {source_id}: Apple's catalogue can't be searched right now ({e}); "
                                 f"the remaining songs Deezer doesn't have get no length this sync.")
                continue
        else:
            continue
        if apple is None:
            t.match_without_length = True
            logger.log_event(f"Amazon Music {source_id}: neither Deezer nor Apple has '{t.primary_artist} - "
                             f"{t.name}', so only a strict match will be accepted for it.")
            continue
        t.duration_ms = int(apple.get("trackTimeMillis") or 0)
        if not album:
            t.album_name = apple.get("collectionName") or t.album_name
        year = (apple.get("releaseDate") or "")[:4]
        t.year = t.year or (year if year.isdigit() else None)
        if not t.cover_url and apple.get("artworkUrl100"):
            t.cover_url = re.sub(r"/\d+x\d+bb\.", "/600x600bb.", apple["artworkUrl100"])


def _tracks(source_id: str, embed: _Embed, album: bool,
            skip_details_for: Optional[Collection[str]], details_only_for: Optional[Collection[str]]) -> List[Track]:
    total = len(embed.songs)
    tracks = []
    for position, (asin, title, artist) in enumerate(embed.songs, start=1):
        tracks.append(Track(
            spotify_id=identity(asin),
            name=_clean_title(title),
            artists=[artist] if artist else ([embed.subtitle] if album and embed.subtitle else []),
            album_name=embed.title if album else None,
            cover_url=embed.cover_url if album else None,
            spotify_url=f"https://{_split(source_id)[0]}/tracks/{asin}",
            list_position=position,
            disc_number=1 if album else 0,
            disc_total=1 if album else 0,
            track_total=total if album else 0,
        ))
    skip = set(skip_details_for or ())
    only = set(details_only_for) if details_only_for is not None else None
    wanted = [t for t in tracks if t.spotify_id not in skip and (only is None or t.spotify_id in only)]
    if wanted:
        _fill_lengths(source_id, tracks, wanted, album)
    return tracks


def get_playlist_tracks(
    source_id: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[list] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """An Amazon Music playlist's (or shared user playlist's) songs, in
    order, as Tracks with spotify_id "amz:<ASIN>". The songs that need
    details (not in skip_details_for / only those in details_only_for)
    get a length, album, year and cover from Apple's catalogue, or are
    marked match_without_length. `unavailable` stays empty."""
    return _tracks(source_id, _embed_or_error(source_id, "playlist"), False, skip_details_for, details_only_for)


def get_album_tracks(
    source_id: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[list] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """An Amazon Music album's songs, numbered by their place "of" the
    album's song count with Disc # 1 of 1 (the embed gives no discs), each
    with the album's name, artist and cover; lengths (and the year) as for
    a playlist."""
    return _tracks(source_id, _embed_or_error(source_id, "album"), True, skip_details_for, details_only_for)


def get_album_info(source_id: str) -> AmazonMusicAlbumInfo:
    """An album's name, artist, cover and song count, for naming its folder
    "Artist - Album". Amazon's embed gives no year."""
    embed = _embed_or_error(source_id, "album")
    return AmazonMusicAlbumInfo(album_id=source_id, name=embed.title,
                                artists=[embed.subtitle] if embed.subtitle else [],
                                cover_url=embed.cover_url, total_tracks=len(embed.songs),
                                album_type="ALBUM" if embed.kind == "album" else embed.kind.upper())
