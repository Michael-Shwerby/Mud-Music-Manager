"""
Mud Music Manager - Spotify Reader

Reads public Spotify playlist data directly via SpotipyFree/spotapi --
no spotDL dependency, no Spotify Developer app, no Client ID/Secret, no
login, and no Premium account required. Only works for playlists set to
Public.

This replaces the Spotify-reading half of what spotDL used to do for this
project. See the design doc, Section 2, for why spotDL was dropped.

Checklist item 26 (faster playlist reads, v0.16.19):
  SpotipyFree's track() builds a brand-new spotapi client for every
  call, and each new client first re-downloads Spotify's web-player
  page, its tokens and its JavaScript bundles before making its one
  real request -- about 2-3 seconds of setup per track. The per-track
  detail fetch now shares ONE spotapi client per playlist read (set up
  once, by the first track, then used by all threads), producing the
  exact same data. If the shared client can't be set up, or a single
  call through it fails, that track falls back to the old per-track
  path, then to the basic listing data, as before -- never worse than
  before. Separately, a caller can pass `skip_details_for` (the IDs
  already downloaded) so those tracks are built from the playlist
  listing alone, since only a not-yet-downloaded track ever needs its
  album/year/cover art.

Unreadable playlist items (v0.16.37):
  SpotipyFree's playlist_items() silently drops any item its formatter
  can't read -- e.g. a track not available in this country, which
  Spotify sends with no name, artist or length. That made every later
  track's position one lower than Spotify's. The listing is now read
  page by page here (the same spotapi pages and formatter SpotipyFree
  uses), so such an item keeps its position slot (Track # always equals
  the Spotify position) and is written to the session log. It still
  can't be downloaded: there is nothing to search for.

  v0.16.39 (Checklist items 45 / 46): a caller can also pass an
  `unavailable` list to get_playlist_tracks(), which receives one
  UnavailableItem (position, Spotify ID, Spotify's reason) per such
  item, so the GUI can show them instead of only the session log.

Albums (v0.16.48, Checklist item 41a):
  get_album_tracks() reads a public Spotify album (spotapi.PublicAlbum,
  no login) into the same Track objects. The album listing already holds
  each track's name, artists, length, disc and track number, and the
  album's own name, date and cover are shared by every track, so no
  per-track detail lookup is needed. An album Track's list_position is
  its track number on its disc (what Track # is set to), and disc_number /
  disc_total / track_total carry the Disc # and the per-disc "of total".
  A playlist Track leaves those three at 0. get_album_info() reads just
  the album's name, artists, year, cover and track count (for naming
  the folder "Artist - Album" when one is added).

Artists (v0.16.50, Checklist items 41b / 41c):
  get_artist_discography() reads an artist's name, picture and releases
  (albums, then singles & EPs, each newest first; Michael's choice) as
  ArtistInfo / Release, for the discography picker and for watching an
  artist for new releases.

Faster launch (v0.23.3):
  spotapi and SpotipyFree (which itself imports spotapi) are imported the
  first time a Spotify read actually runs, not when this module is
  imported. Nearly every other module imports this one just for Track /
  AlbumInfo / ArtistInfo / Release, and spotapi brings in a large chain
  of libraries (pymongo, redis, curl_cffi, ...) that took ~0.5-1 s of
  every launch before the window could appear. The reads themselves are
  unchanged; the first one in a session pays that one-off import cost
  instead (on a background thread in the GUI).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Collection, List, Optional, Tuple

import logger

# v0.23.3: spotapi / SpotipyFree are imported inside the functions that use
# them (see "Faster launch" above), so the annotations below are strings.
_client: Optional["Spotify"] = None


@dataclass
class Track:
    """A single track's Spotify truth data, plus its live position in
    the playlist it was read from."""
    spotify_id: str
    name: str
    artists: List[str] = field(default_factory=list)
    album_name: Optional[str] = None
    year: Optional[str] = None
    duration_ms: int = 0
    cover_url: Optional[str] = None
    spotify_url: str = ""
    list_position: int = 0  # 1-indexed position in the playlist (live order)
    # Album tracks only (v0.16.48, Checklist item 41a); 0 for a playlist
    # track. list_position is then the track number on its disc.
    disc_number: int = 0
    disc_total: int = 0   # how many discs the album has
    track_total: int = 0  # how many tracks are on this track's disc
    # v0.16.54 (Checklist item 42d): an Amazon Music song whose length
    # couldn't be found (Apple's catalogue doesn't have it). With no length
    # to check, only a strict match is accepted (youtube_matcher.
    # find_strict_match(), Michael's choice). Always False for Spotify.
    match_without_length: bool = False

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000 if self.duration_ms else 0.0


# Spotify's playability reasons, in words (v0.16.39). Anything else is
# shown as Spotify wrote it, made readable.
_REASON_TEXT = {
    "COUNTRY_RESTRICTED": "Not available in your country",
    "NOT_AVAILABLE": "Not available on Spotify",
    "UNAVAILABLE": "Not available on Spotify",
    "AGE_RESTRICTED": "Age-restricted on Spotify",
    "EXPLICIT_CONTENT": "Explicit content is turned off",
}


@dataclass
class UnavailableItem:
    """A playlist item Spotify gives no track details for (e.g. not
    available in this country), v0.16.39 (Checklist items 45 / 46). It
    keeps its position slot but can't be downloaded."""
    list_position: int
    spotify_id: str = ""
    name: str = ""      # usually empty: Spotify sends no name for these
    reason: str = ""    # Spotify's playability reason, e.g. COUNTRY_RESTRICTED

    @property
    def reason_text(self) -> str:
        if not self.reason:
            return "Spotify gives no details for it"
        return _REASON_TEXT.get(self.reason, self.reason.replace("_", " ").capitalize())


def _get_client() -> "Spotify":
    from SpotipyFree import Spotify

    global _client
    if _client is None:
        _client = Spotify()
    return _client


def _extract_year(release_date: Optional[str]) -> Optional[str]:
    if not release_date:
        return None
    year = release_date.split("-")[0]
    return year or None


def _extract_cover_url(album: dict) -> Optional[str]:
    images = (album or {}).get("images") or []
    if not images:
        return None
    try:
        # Images aren't guaranteed to be ordered by size -- pick the
        # largest available rather than assuming index 0 is biggest.
        return max(images, key=lambda img: img.get("width") or 0).get("url")
    except Exception:
        return images[0].get("url")


def _new_shared_song() -> Optional["spotapi.Song"]:
    """One spotapi client for a whole playlist read (Checklist item 26).
    It gets its own HTTP session, so its login hooks never mix with any
    other spotapi object's. Returns None if it can't be created."""
    try:
        import spotapi
        from spotapi.http.request import TLSClient

        return spotapi.Song(client=TLSClient("chrome120", "", auto_retries=3))
    except Exception as e:
        logger.log_error(f"Couldn't set up a shared Spotify client, using one per track: {e}")
        return None


def _close_shared_song(song: Optional["spotapi.Song"]) -> None:
    if song is None:
        return
    try:
        song.base.client.close()
    except Exception:
        pass


def _shared_track(song: "spotapi.Song", spotify_id: str) -> dict:
    """Same result as SpotipyFree's Spotify.track() (same request, same
    formatting), but through the shared client instead of a new one."""
    from SpotipyFree.Formatter import SpotifyFormatter

    track = song.get_track_info(spotify_id)["data"]["trackUnion"]
    try:
        artists = track["firstArtist"]["items"]
        artists.extend(track["otherArtists"]["items"])
    except Exception:
        artists = ["Not Found"]
    return SpotifyFormatter.formatTrack(track, SpotifyFormatter.formatArtists(artists))


def _fetch_full_track(
    client: "Spotify", spotify_id: str, fallback: dict, song: Optional["spotapi.Song"] = None
) -> dict:
    """Fetch full track details (fills in album/year/cover art, which
    the playlist listing alone doesn't include). Tries the shared client
    first, then the old one-client-per-track path. Falls back to the
    basic listing data if both fail for this one track, rather than
    failing the whole playlist over one bad track."""
    if song is not None:
        try:
            return _shared_track(song, spotify_id)
        except Exception as e:
            logger.log_event(f"Shared Spotify client failed for track {spotify_id}, retrying the old way: {e}")
    try:
        return client.track(spotify_id)
    except Exception as e:
        logger.log_error(f"Failed to fetch details for track {spotify_id}: {e}")
        return fallback


def _skipped_item(item: dict, position: int) -> UnavailableItem:
    """What can be told about an item that couldn't be read: its Spotify
    ID, a name if Spotify sent one, and Spotify's playability reason
    (e.g. COUNTRY_RESTRICTED)."""
    try:
        data = (item.get("itemV2") or {}).get("data") or (item.get("itemV3") or {}).get("data") or {}
        uri = data.get("uri") or ""
        return UnavailableItem(
            list_position=position,
            spotify_id=uri.rsplit(":", 1)[-1] if uri.startswith("spotify:") else "",
            name=data.get("name") or "",
            reason=(data.get("playability") or {}).get("reason") or "",
        )
    except Exception:
        return UnavailableItem(list_position=position)


def _skipped_item_note(skipped: UnavailableItem) -> str:
    """The track URI and Spotify's playability reason, for the log (e.g.
    "spotify:track:..., COUNTRY_RESTRICTED")."""
    uri = f"spotify:track:{skipped.spotify_id}" if skipped.spotify_id else "unknown track"
    return f"{uri}, {skipped.reason}" if skipped.reason else uri


def _read_listing(spotify_playlist_id: str) -> Tuple[List[Tuple[int, dict]], List[UnavailableItem]]:
    """([(position, basic track)] for every readable item of the
    playlist, in live order; [UnavailableItem] for the rest). position is
    the item's 1-indexed Spotify position, counting items that couldn't
    be read (v0.16.37; see the module docstring), which are logged and
    returned separately (v0.16.39)."""
    import spotapi
    from SpotipyFree.Formatter import SpotifyFormatter

    listing = []
    unavailable = []
    position = 0
    for chunk in spotapi.PublicPlaylist(spotify_playlist_id).paginate_playlist():
        for item in chunk["items"]:
            position += 1
            try:
                listing.append((position, SpotifyFormatter.formatPlaylistTrack(item)["track"]))
            except Exception:
                skipped = _skipped_item(item, position)
                unavailable.append(skipped)
                logger.log_event(
                    f"Spotify playlist {spotify_playlist_id}: position {position} "
                    f"({_skipped_item_note(skipped)}) has no track details (e.g. not available "
                    f"in this country), so it can't be downloaded. Its position is kept "
                    f"so the Track # of later tracks matches Spotify."
                )
    return listing, unavailable


def get_playlist_tracks(
    spotify_playlist_id: str,
    threads: int = 8,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[List[UnavailableItem]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """
    Fetch the full, current track list for a public Spotify playlist, in
    live playlist order, with full details (album, year, cover art)
    filled in via a per-track detail lookup (done in parallel for speed,
    same pattern spotDL itself used, through one shared client --
    Checklist item 26).

    skip_details_for: Spotify Track IDs that don't need the detail
    lookup (Checklist item 26) -- normally the ones already downloaded,
    which a Sync/Scan only renumbers or counts. Those tracks are built
    from the playlist listing alone: ID, name, artists, duration and
    position are all there; album, year and cover art are left empty.
    None (the default) fetches details for every track, as before.

    unavailable: an optional list that receives an UnavailableItem for
    each playlist item Spotify gives no details for (v0.16.39, Checklist
    items 45 / 46), in position order. Those items are never in the
    returned tracks.

    details_only_for: if given, ONLY these Spotify Track IDs get the
    detail lookup (v0.16.40) -- saving an exception or an import only
    tags those tracks, so the rest of a large playlist isn't looked up.

    Raises RuntimeError with a clear, user-facing message if the
    playlist can't be read (not public, wrong ID, empty, etc).
    """
    client = _get_client()
    if client.isUrl(spotify_playlist_id):
        spotify_playlist_id = client.urlToId(spotify_playlist_id)

    try:
        listing, skipped = _read_listing(spotify_playlist_id)
    except Exception as e:
        logger.log_error(f"Failed to read Spotify playlist {spotify_playlist_id}: {e}")
        raise RuntimeError(
            f"Could not read Spotify playlist '{spotify_playlist_id}'. "
            f"Double check the ID is correct and that the playlist is "
            f"set to Public. ({e})"
        ) from e

    if not listing:
        raise RuntimeError(
            f"Spotify playlist '{spotify_playlist_id}' returned no tracks. "
            f"It may be empty, private, or the ID may be wrong."
        )
    if unavailable is not None:
        unavailable.extend(skipped)

    # Basic info (with playlist position) is available immediately; full
    # per-track details are fetched in parallel to keep this fast on
    # large playlists.
    basics = [(position, basic) for position, basic in listing if basic.get("id")]

    skip = set(skip_details_for or ())
    only = set(details_only_for) if details_only_for is not None else None
    to_fetch = [basic for _, basic in basics
                if basic["id"] not in skip and (only is None or basic["id"] in only)]

    full_by_id = {}
    if to_fetch:
        song = _new_shared_song()
        try:
            # The first track sets the shared client up (session, tokens,
            # query hashes) on its own, so the parallel calls after it
            # never race to do that setup at the same time.
            first = to_fetch[0]
            full_by_id[first["id"]] = _fetch_full_track(client, first["id"], first, song)
            with ThreadPoolExecutor(max_workers=threads) as pool:
                future_to_id = {
                    pool.submit(_fetch_full_track, client, basic["id"], basic, song): basic["id"]
                    for basic in to_fetch[1:]
                }
                for future in as_completed(future_to_id):
                    spotify_id = future_to_id[future]
                    full_by_id[spotify_id] = future.result()
        finally:
            _close_shared_song(song)

    tracks: List[Track] = []
    for position, basic in basics:
        full = full_by_id.get(basic["id"], basic)
        album = full.get("album") or {}
        artist_names = [
            a.get("name", "") for a in full.get("artists", []) if a.get("name")
        ]

        tracks.append(Track(
            spotify_id=basic["id"],
            name=full.get("name") or basic.get("name", ""),
            artists=artist_names,
            album_name=album.get("name"),
            year=_extract_year(album.get("release_date")),
            duration_ms=full.get("duration_ms") or basic.get("duration_ms", 0),
            cover_url=_extract_cover_url(album),
            spotify_url=full.get("external_urls", {}).get("spotify", ""),
            list_position=position,
        ))

    tracks.sort(key=lambda t: t.list_position)
    return tracks


# ---------------------------------------------------------------------------
# Albums (v0.16.48, Checklist item 41a)
# ---------------------------------------------------------------------------

# spotapi's largest page for an album read.
_ALBUM_PAGE = 343


@dataclass
class AlbumInfo:
    """An album's own details (v0.16.48)."""
    album_id: str
    name: str
    artists: List[str] = field(default_factory=list)
    year: Optional[str] = None
    cover_url: Optional[str] = None
    total_tracks: int = 0
    album_type: str = ""  # Spotify's type: ALBUM, SINGLE, EP, COMPILATION

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def folder_name(self) -> str:
        """The default name for its folder: "Artist - Album" (Michael's
        choice, 2026-10-09), made safe for a folder name on Windows."""
        name = f"{self.primary_artist} - {self.name}" if self.primary_artist else self.name
        name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
        return name.strip().rstrip(".") or "Album"


def _album_id_of(album_input: str) -> str:
    """The bare album ID of an album link, URI or ID."""
    text = (album_input or "").strip()
    for sep in ("album/", "album:"):
        if sep in text:
            text = text.split(sep, 1)[1]
    return text.split("?", 1)[0].split("/", 1)[0].strip()


def _profile_names(artists: dict) -> List[str]:
    return [a.get("profile", {}).get("name", "") for a in (artists or {}).get("items") or []
            if a.get("profile", {}).get("name")]


def _album_cover_url(album: dict) -> Optional[str]:
    sources = (album.get("coverArt") or {}).get("sources") or []
    if not sources:
        return None
    return max(sources, key=lambda s: s.get("width") or 0).get("url")


def _read_album(album_id: str, all_pages: bool) -> Tuple[dict, list]:
    """(the album's data, [its track items]) from spotapi, one shared
    HTTP session for every page. Raises RuntimeError with a user-facing
    message if the album can't be read."""
    import spotapi
    from spotapi.http.request import TLSClient

    client = TLSClient("chrome120", "", auto_retries=3)
    try:
        reader = spotapi.PublicAlbum(album_id, client=client)
        album = reader.get_album_info(limit=_ALBUM_PAGE if all_pages else 1)["data"]["albumUnion"]
        if not album or not album.get("name"):
            raise ValueError("Spotify returned no album")
        items = list((album.get("tracksV2") or {}).get("items") or [])
        total = (album.get("tracksV2") or {}).get("totalCount") or len(items)
        offset = len(items)
        while all_pages and offset < total:
            page = reader.get_album_info(limit=_ALBUM_PAGE, offset=offset)["data"]["albumUnion"]
            more = (page.get("tracksV2") or {}).get("items") or []
            if not more:
                break
            items.extend(more)
            offset += len(more)
        return album, items
    except Exception as e:
        logger.log_error(f"Failed to read Spotify album {album_id}: {e}")
        raise RuntimeError(
            f"Could not read Spotify album '{album_id}'. Double check the link is correct. ({e})"
        ) from e
    finally:
        try:
            client.close()
        except Exception:
            pass


def _album_info(album_id: str, album: dict) -> AlbumInfo:
    date = album.get("date") or {}
    year = str(date.get("year") or "") or _extract_year((date.get("isoString") or "")[:10])
    return AlbumInfo(
        album_id=album_id,
        name=album.get("name") or "",
        artists=_profile_names(album.get("artists")),
        year=year or None,
        cover_url=_album_cover_url(album),
        total_tracks=(album.get("tracksV2") or {}).get("totalCount") or 0,
        album_type=album.get("type") or "",
    )


def get_album_info(album_input: str) -> AlbumInfo:
    """An album's name, artists, year, cover and track count (v0.16.48),
    without reading its tracks. Raises RuntimeError if it can't be read."""
    album_id = _album_id_of(album_input)
    album, _items = _read_album(album_id, all_pages=False)
    return _album_info(album_id, album)


def get_album_tracks(
    album_input: str,
    skip_details_for: Optional[Collection[str]] = None,
    unavailable: Optional[List[UnavailableItem]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[Track]:
    """
    The tracks of a public Spotify album, in album order (disc, then
    track number), as Track objects (v0.16.48, Checklist item 41a). Takes
    the same arguments as get_playlist_tracks() so callers can use either;
    skip_details_for / details_only_for change nothing here, since the
    album listing already has every detail (the album, year and cover are
    the album's own). A track Spotify doesn't play here still has its
    name, artists and length, so it is a normal track (searched for on
    YouTube like any other). Only an item that can't be read at all goes
    to `unavailable`, at its track number.

    Raises RuntimeError with a clear, user-facing message if the album
    can't be read or has no tracks.
    """
    album_id = _album_id_of(album_input)
    album, items = _read_album(album_id, all_pages=True)
    info = _album_info(album_id, album)

    disc_sizes = {}
    for disc in (album.get("discs") or {}).get("items") or []:
        try:
            disc_sizes[int(disc.get("number") or 0)] = int((disc.get("tracks") or {}).get("totalCount") or 0)
        except (TypeError, ValueError):
            continue

    parsed = []
    skipped = []
    for index, item in enumerate(items, start=1):
        data = (item or {}).get("track") or {}
        uri = data.get("uri") or ""
        try:
            track_number = int(data.get("trackNumber") or 0) or index
            disc_number = int(data.get("discNumber") or 1)
        except (TypeError, ValueError):
            track_number, disc_number = index, 1
        name = data.get("name") or ""
        duration_ms = int((data.get("duration") or {}).get("totalMilliseconds") or 0)
        spotify_id = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else ""
        if not (spotify_id and name and duration_ms):
            reason = (data.get("playability") or {}).get("reason") or ""
            skipped.append(UnavailableItem(list_position=track_number, spotify_id=spotify_id,
                                           name=name, reason=reason))
            logger.log_event(
                f"Spotify album {album_id}: track {disc_number}-{track_number} "
                f"({spotify_id or 'unknown track'}) has no track details, so it can't be downloaded."
            )
            continue
        parsed.append(Track(
            spotify_id=spotify_id,
            name=name,
            artists=_profile_names(data.get("artists")) or list(info.artists),
            album_name=info.name,
            year=info.year,
            duration_ms=duration_ms,
            cover_url=info.cover_url,
            spotify_url=f"https://open.spotify.com/track/{spotify_id}",
            list_position=track_number,
            disc_number=disc_number,
        ))

    if not parsed:
        raise RuntimeError(f"Spotify album '{album_id}' returned no tracks.")
    if unavailable is not None:
        unavailable.extend(skipped)

    disc_total = max([t.disc_number for t in parsed] + list(disc_sizes) + [1])
    for t in parsed:
        t.disc_total = disc_total
        t.track_total = disc_sizes.get(t.disc_number) or max(
            u.list_position for u in parsed if u.disc_number == t.disc_number)
    parsed.sort(key=lambda t: (t.disc_number, t.list_position))
    return parsed


# ---------------------------------------------------------------------------
# Artists (v0.16.50, Checklist items 41b / 41c)
# ---------------------------------------------------------------------------

# Spotify's release types, in words.
RELEASE_TYPE_TEXT = {"ALBUM": "Album", "SINGLE": "Single", "EP": "EP", "COMPILATION": "Compilation"}

# The discography sections offered (Michael's choice, 2026-10-09): albums,
# and singles & EPs. Not compilations or appears-on.
DISCOGRAPHY_SECTIONS = ("albums", "singles")


@dataclass
class Release:
    """One release in an artist's discography (v0.16.50)."""
    album_id: str
    name: str
    release_type: str = ""   # ALBUM, SINGLE, EP, COMPILATION
    year: Optional[str] = None
    total_tracks: int = 0
    cover_url: Optional[str] = None
    section: str = ""        # the discography section it was listed in: albums / singles

    @property
    def type_text(self) -> str:
        return RELEASE_TYPE_TEXT.get(self.release_type, self.release_type.capitalize() or "Release")

    @property
    def link(self) -> str:
        return f"https://open.spotify.com/album/{self.album_id}"


@dataclass
class ArtistInfo:
    """An artist and its albums / singles & EPs, newest first (v0.16.50)."""
    artist_id: str
    name: str
    image_url: Optional[str] = None
    releases: List[Release] = field(default_factory=list)

    @property
    def link(self) -> str:
        return f"https://open.spotify.com/artist/{self.artist_id}"


def _artist_id_of(artist_input: str) -> str:
    text = (artist_input or "").strip()
    for sep in ("artist/", "artist:"):
        if sep in text:
            text = text.split(sep, 1)[1]
    return text.split("?", 1)[0].split("/", 1)[0].strip()


def _release_of(item: dict, section: str) -> Optional[Release]:
    """A discography item -> Release (its first release: an item can list
    alternative versions of the same release)."""
    try:
        r = ((item or {}).get("releases") or {}).get("items")[0]
    except (TypeError, IndexError):
        return None
    album_id = r.get("id") or (r.get("uri") or "").rsplit(":", 1)[-1]
    if not album_id or not r.get("name"):
        return None
    year = (r.get("date") or {}).get("year")
    return Release(
        album_id=album_id, name=r["name"], release_type=r.get("type") or "",
        year=str(year) if year else None,
        total_tracks=int((r.get("tracks") or {}).get("totalCount") or 0),
        cover_url=_album_cover_url(r), section=section,
    )


def get_artist_discography(artist_input: str,
                           sections: Collection[str] = DISCOGRAPHY_SECTIONS) -> ArtistInfo:
    """
    An artist's name, picture and releases (v0.16.50, Checklist item
    41b): its albums, then its singles & EPs, each newest first, read
    with spotapi (public data, no login). Raises RuntimeError with a
    user-facing message if the artist can't be read.
    """
    import spotapi
    from spotapi.http.request import TLSClient

    artist_id = _artist_id_of(artist_input)
    client = TLSClient("chrome120", "", auto_retries=3)
    try:
        reader = spotapi.Artist(client=client)
        overview = reader.get_artist(artist_id)["data"]["artistUnion"]
        name = (overview.get("profile") or {}).get("name") or ""
        if not name:
            raise ValueError("Spotify returned no artist")
        sources = ((overview.get("visuals") or {}).get("avatarImage") or {}).get("sources") or []
        image = max(sources, key=lambda s: s.get("width") or 0).get("url") if sources else None
        releases, seen = [], set()
        for section in sections:
            for page in reader.paginate_artist_discography(artist_id, section=section):
                for item in page:
                    release = _release_of(item, section)
                    if release and release.album_id not in seen:
                        seen.add(release.album_id)
                        releases.append(release)
        return ArtistInfo(artist_id=artist_id, name=name, image_url=image, releases=releases)
    except Exception as e:
        logger.log_error(f"Failed to read Spotify artist {artist_id}: {e}")
        raise RuntimeError(
            f"Could not read Spotify artist '{artist_id}'. Double check the link is correct. ({e})"
        ) from e
    finally:
        try:
            client.close()
        except Exception:
            pass
