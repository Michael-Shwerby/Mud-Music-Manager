"""
Mud Music Manager - Apple Music Reader (v0.16.53, Checklist item 42c)

Reads Apple Music playlists, albums and artists without a login or an
Apple developer account. Apple Music's songs can't be downloaded (DRM),
so -- Michael's choice, 2026-10-09 -- Apple Music works like Spotify: the
playlist is read here, and each song is found on YouTube with the usual
tiered search (youtube_matcher, §3.4). Checked live, 2026-10-09:

  - A public playlist's page (music.apple.com/<country>/playlist/...)
    carries its whole track list in the page itself, as JSON
    ("serialized-server-data"): each song's title, artist, length (ms),
    album and Apple's song ID, in order (all 249 songs of the largest
    playlist tried). The name part of the link doesn't matter.
  - Apple's public iTunes lookup API (itunes.apple.com/lookup) gives a
    song's album, release date, disc and artwork, up to 150 songs per call
    -- used for the playlist songs that need details (not downloaded yet)
    -- and an album's tracks with disc / track numbers, and an artist's
    albums and singles & EPs.

Every ID here carries the store country of its link ("us", "gb"...),
since Apple's catalogue (and so its IDs' availability) is per country:
a playlist "us/pl.xxxx", an album "us/697194953", an artist
"us/artist/5468295" (config_manager.extract_apple_music_*()).

Songs are spotify_reader.Track objects, so the whole Spotify path
(search, download, tag, fix, import) handles them unchanged; their
`spotify_id` field holds the song's identity, "am:<Apple song ID>" (the
Comment tag), not a Spotify ID.
"""

import re
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Tuple

import logger
from spotify_reader import AlbumInfo, ArtistInfo, Release, Track

IDENTITY_PREFIX = "am:"
_LOOKUP = "https://itunes.apple.com/lookup"
_LOOKUP_BATCH = 150
_COVER_SIZE = "600x600bb"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def identity(song_id) -> str:
    return f"{IDENTITY_PREFIX}{song_id}"


def _split(source_id: str) -> Tuple[str, str]:
    """("us", "pl.xxxx" / "697194953" / "artist/5468295") of a source ID."""
    country, _, rest = (source_id or "").partition("/")
    return country or "us", rest


def playlist_link(source_id: str) -> str:
    country, pid = _split(source_id)
    return f"https://music.apple.com/{country}/playlist/{pid}"


def album_link(source_id: str) -> str:
    country, aid = _split(source_id)
    return f"https://music.apple.com/{country}/album/{aid}"


def artist_link(source_id: str) -> str:
    country, rest = _split(source_id)
    return f"https://music.apple.com/{country}/artist/{rest.split('/', 1)[-1]}"


@dataclass
class AppleMusicAlbumInfo(AlbumInfo):
    @property
    def link(self) -> str:
        return album_link(self.album_id)


@dataclass
class AppleMusicRelease(Release):
    @property
    def link(self) -> str:
        return album_link(self.album_id)


@dataclass
class AppleMusicArtistInfo(ArtistInfo):
    @property
    def link(self) -> str:
        return artist_link(self.artist_id)


def _get(url: str, **params):
    import requests
    response = requests.get(url, params=params or None, headers=_HEADERS, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response


def _lookup(country: str, **params) -> List[dict]:
    return _get(_LOOKUP, country=country, **params).json().get("results") or []


def _cover(url: Optional[str]) -> Optional[str]:
    """An artwork link at 600 px: lookup's ".../100x100bb.jpg", or the
    page's ".../{w}x{h}bb.{f}" template."""
    if not url:
        return None
    url = url.replace("{w}x{h}bb", _COVER_SIZE).replace("{f}", "jpg")
    return re.sub(r"/\d+x\d+bb\.", f"/{_COVER_SIZE}.", url)


def _year(date: Optional[str]) -> Optional[str]:
    return date[:4] if date and re.match(r"\d{4}", date) else None


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def _page_data(url: str) -> dict:
    html = _get(url).text
    match = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("the page holds no playlist data (is it public?)")
    import json
    data = json.loads(match.group(1))
    return data["data"][0]["data"]


def _song_id(item: dict) -> Optional[str]:
    ident = ((item.get("contentDescriptor") or {}).get("identifiers") or {}).get("storeAdamID")
    return str(ident) if ident else None


def _song_details(country: str, song_ids: List[str]) -> Dict[str, dict]:
    """{song ID: lookup result} (album, release date, disc, artwork), in
    batches. A failed batch only means those songs get no year / cover."""
    found = {}
    for start in range(0, len(song_ids), _LOOKUP_BATCH):
        batch = song_ids[start:start + _LOOKUP_BATCH]
        try:
            for r in _lookup(country, id=",".join(batch)):
                if r.get("wrapperType") == "track":
                    found[str(r.get("trackId"))] = r
        except Exception as e:
            logger.log_error(f"Apple Music: couldn't look up the details of {len(batch)} songs: {e}")
    return found


def get_playlist_tracks(
    source_id: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[list] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """
    A public Apple Music playlist's songs, in order, as Tracks whose
    spotify_id is "am:<song ID>". Each has its title, artist, length and
    album name from the page. The songs that need details (not in
    skip_details_for / only those in details_only_for -- identities, as
    the other readers take them) get their release year and cover from
    one lookup per 150 songs. `unavailable` is accepted to match
    spotify_reader and stays empty: a song Apple Music can't play here is
    still searched for on YouTube, as for a Spotify album.
    Raises RuntimeError with a user-facing message if it can't be read.
    """
    country, pid = _split(source_id)
    try:
        sections = _page_data(playlist_link(source_id))["sections"]
        header = (sections[0].get("items") or [{}])[0]
        items = [i for s in sections if s.get("itemKind") == "trackLockup" for i in s.get("items") or []]
        if not items:
            raise ValueError("the playlist has no songs, or isn't public")
    except Exception as e:
        logger.log_error(f"Failed to read Apple Music playlist {source_id}: {e}")
        raise RuntimeError(
            f"Could not read Apple Music playlist '{source_id}'. Double check the link is right and the "
            f"playlist is public (shared). ({e})"
        ) from e
    expected = header.get("trackCount")
    if isinstance(expected, int) and expected > len(items):
        logger.log_event(f"Apple Music playlist {source_id}: its page lists {len(items)} of {expected} songs; "
                         f"only those can be synced.")

    skip = set(skip_details_for or ())
    only = set(details_only_for) if details_only_for is not None else None
    tracks: List[Track] = []
    for position, item in enumerate(items, start=1):
        song_id = _song_id(item)
        if not song_id or not item.get("title"):
            logger.log_event(f"Apple Music playlist {source_id}: song {position} gives no details, so it "
                             f"can't be searched for.")
            continue
        album_links = item.get("tertiaryLinks") or []
        tracks.append(Track(
            spotify_id=identity(song_id),
            name=item["title"],
            artists=[item.get("artistName")] if item.get("artistName") else [],
            album_name=(album_links[0].get("title") if album_links else None),
            duration_ms=int(item.get("duration") or 0),
            spotify_url=f"https://music.apple.com/{country}/song/{song_id}",
            list_position=position,
        ))
    want = [t.spotify_id[len(IDENTITY_PREFIX):] for t in tracks
            if t.spotify_id not in skip and (only is None or t.spotify_id in only)]
    details = _song_details(country, want) if want else {}
    for t in tracks:
        d = details.get(t.spotify_id[len(IDENTITY_PREFIX):])
        if d:
            t.album_name = d.get("collectionName") or t.album_name
            t.year = _year(d.get("releaseDate"))
            t.cover_url = _cover(d.get("artworkUrl100"))
    if not tracks:
        raise RuntimeError(f"Apple Music playlist '{source_id}' returned no songs.")
    return tracks


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------

def _read_album(source_id: str) -> Tuple[dict, List[dict]]:
    country, aid = _split(source_id)
    try:
        results = _lookup(country, id=aid, entity="song", limit=300)
        album = next((r for r in results if r.get("wrapperType") == "collection"), None)
        songs = [r for r in results if r.get("wrapperType") == "track"]
        if not album:
            raise ValueError("Apple Music returned no album")
        return album, songs
    except Exception as e:
        logger.log_error(f"Failed to read Apple Music album {source_id}: {e}")
        raise RuntimeError(
            f"Could not read Apple Music album '{source_id}'. Double check the link is right. ({e})"
        ) from e


def _album_info(source_id: str, album: dict) -> AppleMusicAlbumInfo:
    name = album.get("collectionName") or ""
    return AppleMusicAlbumInfo(
        album_id=source_id,
        name=name,
        artists=[album["artistName"]] if album.get("artistName") else [],
        year=_year(album.get("releaseDate")),
        cover_url=_cover(album.get("artworkUrl100")),
        total_tracks=int(album.get("trackCount") or 0),
        album_type=_release_type(name, album.get("trackCount")),
    )


def get_album_info(source_id: str) -> AppleMusicAlbumInfo:
    """An album's name, artist, year, cover and track count (to name its
    folder "Artist - Album")."""
    album, _songs = _read_album(source_id)
    return _album_info(source_id, album)


def get_album_tracks(
    source_id: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[list] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """An Apple Music album's songs in album order (disc, then track), as
    Tracks with spotify_id "am:<song ID>", each with the album's name,
    year and cover, its track number (list_position), Disc # and its disc's
    track count -- one lookup call. The details arguments change nothing
    (the lookup has everything); `unavailable` stays empty."""
    album, songs = _read_album(source_id)
    info = _album_info(source_id, album)
    if not songs:
        raise RuntimeError(f"Apple Music album '{source_id}' returned no songs.")
    tracks = []
    for s in songs:
        tracks.append(Track(
            spotify_id=identity(s.get("trackId")),
            name=s.get("trackName") or "",
            artists=[s["artistName"]] if s.get("artistName") else list(info.artists),
            album_name=info.name,
            year=info.year,
            duration_ms=int(s.get("trackTimeMillis") or 0),
            cover_url=info.cover_url,
            spotify_url=s.get("trackViewUrl") or "",
            list_position=int(s.get("trackNumber") or 0),
            disc_number=int(s.get("discNumber") or 1),
            disc_total=int(s.get("discCount") or 1),
            track_total=int(s.get("trackCount") or 0),
        ))
    tracks.sort(key=lambda t: (t.disc_number, t.list_position))
    return tracks


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------

def _release_type(name: str, track_count=None) -> str:
    if name.endswith(" - Single"):
        return "SINGLE"
    if name.endswith(" - EP"):
        return "EP"
    return "ALBUM"


def _clean_name(name: str) -> str:
    """"Get Lucky - Single" -> "Get Lucky" (the type is shown separately)."""
    return re.sub(r" - (Single|EP)$", "", name or "")


def get_artist_discography(source_id: str) -> AppleMusicArtistInfo:
    """An Apple Music artist's name and releases: its Albums, then its
    Singles & EPs (Michael's discography choice), newest first, from one
    lookup call. Apple gives no artist picture there, so the artist's bar
    shows a generated one."""
    country, rest = _split(source_id)
    artist_number = rest.split("/", 1)[-1]
    try:
        results = _lookup(country, id=artist_number, entity="album", limit=200)
        artist = next((r for r in results if r.get("wrapperType") == "artist"), None)
        if not artist:
            raise ValueError("Apple Music returned no artist")
        releases = []
        for r in results:
            if r.get("wrapperType") != "collection" or r.get("collectionType") not in (None, "Album"):
                continue
            name = r.get("collectionName") or ""
            kind = _release_type(name)
            releases.append(AppleMusicRelease(
                album_id=f"{country}/{r['collectionId']}", name=_clean_name(name), release_type=kind,
                year=_year(r.get("releaseDate")), total_tracks=int(r.get("trackCount") or 0),
                cover_url=_cover(r.get("artworkUrl100")),
                section="albums" if kind == "ALBUM" else "singles",
            ))
        releases.sort(key=lambda x: x.year or "", reverse=True)
        releases.sort(key=lambda x: x.section != "albums")  # stable: albums first, each newest first
        return AppleMusicArtistInfo(artist_id=source_id, name=artist.get("artistName") or "",
                                    image_url=None, releases=releases)
    except Exception as e:
        logger.log_error(f"Failed to read Apple Music artist {source_id}: {e}")
        raise RuntimeError(
            f"Could not read Apple Music artist '{source_id}'. Double check the link is right. ({e})"
        ) from e
