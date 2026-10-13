"""
Mud Music Manager - YT Music Reader (v0.16.51, Checklist item 42a)

Reads YT Music playlists, albums and artists directly with ytmusicapi --
no login, public data only (checked live, 2026-10-09). The YT Music
counterpart of spotify_reader.py (albums, artists) and
youtube_playlist_reader.py (videos to download as listed).

YT Music is its own platform (Michael's choice), but its songs are YouTube
videos: each one is a youtube_playlist_reader.YouTubeTrack, identified by
"yt:<videoId>" and downloaded directly, with no search (as a YouTube
playlist). What YT Music adds is its catalogue data:

  - Playlists (get_playlist_tracks()): YT Music's own title, artists,
    album and length for each song, in playlist order, instead of the
    video's title and channel. For a song that belongs to an album, the
    album is read once (shared by its songs) for the year and the square
    album cover -- only for the songs that need details (not downloaded
    yet), as the other readers do. A video with no album (an ordinary
    upload) keeps its channel as the artist and no album / year, as on
    YouTube. Track # is the playlist position.
  - Albums (get_album_tracks()): the album's tracks in album order, Track #
    the album's own track number "of" its track count, Disc # 1 of 1 (YT
    Music gives no disc numbers; Michael's album rule), the album's name,
    year and cover on every track.
  - Album songs listed as videos (Michael's choice, 2026-10-09): YT Music
    often lists an album's songs as their official music videos (all 14
    of Daft Punk's Discovery, checked live). Such a track is marked
    wants_audio_version, and the album's own audio playlist (its
    audioPlaylistId, "OLAK5uy_...", which lists the audio-only uploads in
    the same order) gives the audio-only (ATV) version when its title
    matches and its length is within 5 seconds: audio_video_id. If none is
    found there, the download searches for one (acquisition_engine); if
    there is none, the listed video is downloaded. The file's identity
    stays the listed video either way, so the diff never changes.
  - Artists (get_artist_discography()): name, picture, Albums and Singles &
    EPs (Michael's choice for discographies), as spotify_reader's ArtistInfo
    / Release, for the discography picker and watching an artist.

Raises RuntimeError with a user-facing message when something can't be
read (private playlist, wrong link, no connection).
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Tuple

from ytmusicapi import YTMusic

import logger
from spotify_reader import AlbumInfo, ArtistInfo, Release
from youtube_playlist_reader import YouTubeTrack

ATV = "MUSIC_VIDEO_TYPE_ATV"
DURATION_TOLERANCE_SECONDS = 5  # as youtube_matcher's
DISCOGRAPHY_SECTIONS = ("albums", "singles")  # Albums, Singles & EPs

_client: Optional[YTMusic] = None
_client_lock = threading.Lock()


def _yt() -> YTMusic:
    global _client
    with _client_lock:
        if _client is None:
            _client = YTMusic(location="US")  # as youtube_matcher's searches
        return _client


@dataclass
class YTMusicAlbumInfo(AlbumInfo):
    """A YT Music album's own details; album_id is its browse ID."""

    @property
    def link(self) -> str:
        return album_link(self.album_id)


@dataclass
class YTMusicRelease(Release):
    @property
    def link(self) -> str:
        return album_link(self.album_id)


@dataclass
class YTMusicArtistInfo(ArtistInfo):
    @property
    def link(self) -> str:
        return artist_link(self.artist_id)


def album_link(album_id: str) -> str:
    if album_id.startswith("OLAK5uy_"):
        return f"https://music.youtube.com/playlist?list={album_id}"
    return f"https://music.youtube.com/browse/{album_id}"


def playlist_link(playlist_id: str) -> str:
    return f"https://music.youtube.com/playlist?list={playlist_id}"


def artist_link(artist_id: str) -> str:
    return f"https://music.youtube.com/channel/{artist_id}"


def _normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _best_image(thumbnails) -> Optional[str]:
    """The largest picture's URL. YT Music's square covers come as small
    googleusercontent links whose size is in the URL, so they are asked
    for at 544 px (the size YT Music itself uses for an album page)."""
    thumbs = [t for t in (thumbnails or []) if t.get("url")]
    if not thumbs:
        return None
    url = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))["url"]
    if "googleusercontent.com" in url:
        url = re.sub(r"=w\d+-h\d+", "=w544-h544", url)
    return url


def _names(artists) -> List[str]:
    return [a.get("name") for a in (artists or []) if a.get("name")]


def _seconds(item: dict) -> int:
    try:
        return int(item.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return 0


def _wants_details(video_id: str, fetch_details: bool, skip: set, only: Optional[set]) -> bool:
    return fetch_details and video_id not in skip and (only is None or video_id in only)


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------

def _browse_id(album_input: str) -> str:
    """The album's browse ID (MPREb_...) for a browse ID, an album's own
    playlist ID (OLAK5uy_..., looked up) or a link holding either."""
    text = (album_input or "").strip()
    match = re.search(r"MPREb_[A-Za-z0-9_-]+", text)
    if match:
        return match.group(0)
    match = re.search(r"OLAK5uy_[A-Za-z0-9_-]+", text)
    if match:
        browse_id = _yt().get_album_browse_id(match.group(0))
        if browse_id:
            return browse_id
    raise ValueError("that isn't a YT Music album link")


def _read_album(album_input: str) -> Tuple[dict, str]:
    """(the album's data, its browse ID)."""
    try:
        browse_id = _browse_id(album_input)
        album = _yt().get_album(browse_id)
        if not album or not album.get("title"):
            raise ValueError("YT Music returned no album")
        return album, browse_id
    except Exception as e:
        logger.log_error(f"Failed to read YT Music album {album_input}: {e}")
        raise RuntimeError(
            f"Could not read YT Music album '{album_input}'. Double check the link is correct. ({e})"
        ) from e


def _album_info(album: dict, album_id: str) -> YTMusicAlbumInfo:
    return YTMusicAlbumInfo(
        album_id=album_id,
        name=album.get("title") or "",
        artists=_names(album.get("artists")),
        year=str(album.get("year") or "") or None,
        cover_url=_best_image(album.get("thumbnails")),
        total_tracks=int(album.get("trackCount") or len(album.get("tracks") or [])),
        album_type=(album.get("type") or "").upper(),
    )


def get_album_info(album_input: str) -> YTMusicAlbumInfo:
    """A YT Music album's name, artists, year, cover and track count (to
    name its folder "Artist - Album"). Its album_id is the browse ID, even
    for an album link given as its own playlist (OLAK5uy_...)."""
    album, browse_id = _read_album(album_input)
    return _album_info(album, browse_id)


def _same_song(listed: dict, candidate: dict) -> bool:
    if candidate.get("videoType") != ATV or not candidate.get("videoId"):
        return False
    if _normalize(listed.get("title")) != _normalize(candidate.get("title")):
        return False
    a, b = _seconds(listed), _seconds(candidate)
    return not (a and b) or abs(a - b) <= DURATION_TOLERANCE_SECONDS


def _audio_versions(album: dict, wanted: List[int]) -> Dict[int, str]:
    """{track index: audio-only video ID} for the album tracks at `wanted`
    (indexes into album["tracks"]), from the album's own audio playlist:
    the same position first, else any track there with the same title and
    length. One playlist read for the album; a failure only means the
    download searches instead (logged)."""
    playlist_id = album.get("audioPlaylistId")
    if not wanted or not playlist_id:
        return {}
    try:
        audio = (_yt().get_playlist(playlist_id, limit=None) or {}).get("tracks") or []
    except Exception as e:
        logger.log_error(f"Couldn't read YT Music album playlist {playlist_id} for audio versions: {e}")
        return {}
    found = {}
    tracks = album.get("tracks") or []
    for i in wanted:
        listed = tracks[i]
        if i < len(audio) and _same_song(listed, audio[i]):
            found[i] = audio[i]["videoId"]
            continue
        match = next((c for c in audio if _same_song(listed, c)), None)
        if match:
            found[i] = match["videoId"]
    return found


def get_album_tracks(
    album_input: str,
    fetch_details: bool = True,
    skip_details_for: Optional[Collection[str]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[YouTubeTrack]:
    """The tracks of a public YT Music album, in album order (see the
    module docstring). The album listing already has every tag, so the
    details arguments only decide which video-listed tracks get their
    audio-only version looked up (one read of the album's audio playlist):
    those not downloaded yet (skip_details_for / details_only_for: bare
    video IDs); none when fetch_details is False (Scan)."""
    album, browse_id = _read_album(album_input)
    info = _album_info(album, browse_id)
    items = album.get("tracks") or []
    total = len(items) or info.total_tracks
    skip = set(skip_details_for or ())
    only = set(details_only_for) if details_only_for is not None else None

    tracks: List[YouTubeTrack] = []
    want_audio = []
    for index, item in enumerate(items):
        video_id = item.get("videoId")
        try:
            number = int(item.get("trackNumber") or 0) or index + 1
        except (TypeError, ValueError):
            number = index + 1
        if not video_id:
            logger.log_event(f"YT Music album {album_input}: track {number} "
                             f"('{item.get('title') or '?'}') has no video, so it can't be downloaded.")
            continue
        wants_audio = bool(item.get("videoType")) and item.get("videoType") != ATV
        if wants_audio and _wants_details(video_id, fetch_details, skip, only):
            want_audio.append(index)
        tracks.append(YouTubeTrack(
            video_id=video_id,
            title=item.get("title") or video_id,
            artists=_names(item.get("artists")) or list(info.artists),
            album_name=info.name,
            year=info.year,
            duration_ms=_seconds(item) * 1000,
            cover_url=info.cover_url,
            channel_name=info.primary_artist,
            list_position=number,
            disc_number=1,
            disc_total=1,
            track_total=total,
            wants_audio_version=wants_audio,
        ))
    if not tracks:
        raise RuntimeError(f"YT Music album '{album_input}' returned no tracks.")

    found = _audio_versions(album, want_audio)
    by_index = {i: t for i, t in zip([i for i, it in enumerate(items) if it.get("videoId")], tracks)}
    for i, video_id in found.items():
        by_index[i].audio_video_id = video_id
    tracks.sort(key=lambda t: t.list_position)
    return tracks


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

def get_playlist_tracks(
    playlist_id: str,
    fetch_details: bool = True,
    skip_details_for: Optional[Collection[str]] = None,
    details_only_for: Optional[Collection[str]] = None,
    threads: int = 4,
) -> List[YouTubeTrack]:
    """A public YT Music playlist's songs, in playlist order (see the
    module docstring). The listing gives each song's title, artists,
    album and length; the albums of the songs that need details (not
    downloaded yet; skip_details_for / details_only_for are bare video
    IDs, fetch_details=False for none) are read once each for the year and
    cover. An item with no video keeps its position but isn't a track."""
    try:
        playlist = _yt().get_playlist(playlist_id, limit=None)
    except Exception as e:
        logger.log_error(f"Failed to read YT Music playlist {playlist_id}: {e}")
        raise RuntimeError(
            f"Could not read YT Music playlist '{playlist_id}'. Double check the link is correct "
            f"and that the playlist is Public or Unlisted. ({e})"
        ) from e
    items = (playlist or {}).get("tracks") or []
    if not items:
        raise RuntimeError(
            f"YT Music playlist '{playlist_id}' returned no songs. It may be empty or private, "
            f"or the link may be wrong."
        )

    skip = set(skip_details_for or ())
    only = set(details_only_for) if details_only_for is not None else None
    album_ids = {(item.get("album") or {}).get("id") for item in items
                 if item.get("videoId") and _wants_details(item["videoId"], fetch_details, skip, only)}
    album_ids.discard(None)
    albums: Dict[str, dict] = {}

    def read(album_id):
        try:
            return album_id, _yt().get_album(album_id)
        except Exception as e:
            logger.log_error(f"Couldn't read YT Music album {album_id} for a playlist song's details: {e}")
            return album_id, None

    if album_ids:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for album_id, album in pool.map(read, sorted(album_ids)):
                if album:
                    albums[album_id] = album

    tracks: List[YouTubeTrack] = []
    for position, item in enumerate(items, start=1):
        video_id = item.get("videoId")
        if not video_id:
            logger.log_event(f"YT Music playlist {playlist_id}: item {position} "
                             f"('{item.get('title') or '?'}') has no video, so it can't be downloaded.")
            continue
        artists = _names(item.get("artists"))
        album_ref = item.get("album") or {}
        album = albums.get(album_ref.get("id"))
        tracks.append(YouTubeTrack(
            video_id=video_id,
            title=item.get("title") or video_id,
            artists=artists,
            album_name=album_ref.get("name") or None,
            year=(str(album.get("year") or "") or None) if album else None,
            duration_ms=_seconds(item) * 1000,
            cover_url=_best_image((album or {}).get("thumbnails")) or _best_image(item.get("thumbnails")),
            channel_name=artists[0] if artists else "",
            list_position=position,
        ))
    return tracks


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------

def _artist_id_of(artist_input: str) -> str:
    match = re.search(r"UC[A-Za-z0-9_-]{22}", artist_input or "")
    return match.group(0) if match else (artist_input or "").strip()


def get_artist_discography(artist_input: str,
                           sections: Collection[str] = DISCOGRAPHY_SECTIONS) -> YTMusicArtistInfo:
    """A YT Music artist's name, picture and releases: its Albums, then its
    Singles & EPs, in YT Music's order (newest first). The artist page only
    previews each section, so a section with more is read in full. Raises
    RuntimeError with a user-facing message if the artist can't be read
    (e.g. the channel isn't an artist's)."""
    artist_id = _artist_id_of(artist_input)
    try:
        artist = _yt().get_artist(artist_id)
        name = (artist or {}).get("name") or ""
        if not name:
            raise ValueError("YT Music returned no artist")
        releases, seen = [], set()
        for section in sections:
            shelf = artist.get(section) or {}
            items = shelf.get("results") or []
            if shelf.get("browseId") and shelf.get("params"):
                items = _yt().get_artist_albums(shelf["browseId"], shelf["params"], limit=None) or items
            for item in items:
                album_id = item.get("browseId")
                if not album_id or not item.get("title") or album_id in seen:
                    continue
                seen.add(album_id)
                default_type = "ALBUM" if section == "albums" else "SINGLE"
                releases.append(YTMusicRelease(
                    album_id=album_id, name=item["title"],
                    release_type=(item.get("type") or default_type).upper(),
                    year=str(item.get("year") or "") or None,
                    cover_url=_best_image(item.get("thumbnails")), section=section,
                ))
        return YTMusicArtistInfo(artist_id=artist_id, name=name,
                                 image_url=_best_image(artist.get("thumbnails")), releases=releases)
    except Exception as e:
        logger.log_error(f"Failed to read YT Music artist {artist_id}: {e}")
        raise RuntimeError(
            f"Could not read YT Music artist '{artist_id}'. Double check it's an artist's link. ({e})"
        ) from e
