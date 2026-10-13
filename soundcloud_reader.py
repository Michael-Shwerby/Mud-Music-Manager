"""
Mud Music Manager - SoundCloud Reader (v0.16.52, Checklist item 42b)

Reads SoundCloud sets -- playlists and albums (Michael's scope for 42b) --
through SoundCloud's own public API, the way yt-dlp's SoundCloud
extractor does (its _call_api() and the anonymous client ID it looks up
and caches). No login. Checked live, 2026-10-09:

  - One "resolve" call reads a set: its title, uploader, release date,
    artwork, whether it is an album (is_album / set_type), and every
    track's ID in order. Track details come in batches of up to 50 (one
    call per 50 songs; SoundCloud returns them in any order, so they are
    put back in the set's order here).
  - Each track has a `policy`. "SNIP" is a 30-second preview (Go+ only:
    official label uploads such as Daft Punk's), "BLOCK" can't be played
    at all; the real length is `full_duration`. Some label uploads are
    also DRM-protected, which yt-dlp only says when downloading. Uploads
    by independent artists and labels (lo-fi, remixes, mixes) are full.

So a SoundCloudTrack carries `preview_only` for SNIP / BLOCK, and
acquisition_engine finds those songs on YouTube instead (Michael's
choice: SoundCloud first, YouTube as the fallback).

Tags: Title is the track's own title. Artist is the publisher's artist
when SoundCloud has one, otherwise the uploader. Album is the set's
title for an album (the publisher's album title, if any, for a playlist
song). Year is the release date's (or the upload's). Cover is the
track's artwork at 500 px (the set's, then the uploader's picture, when
it has none). An album's songs are numbered by their place in the album
"of" its track count, with Disc # 1 of 1 (SoundCloud has no discs).

The songs' identity is "sc:<track id>" (metadata_tagger), so renaming a
track on SoundCloud changes nothing.
"""

import re
import threading
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Tuple

import yt_dlp

import config_manager
import logger

_BATCH = 50
PREVIEW_POLICIES = ("SNIP", "BLOCK")
_local = threading.local()


@dataclass
class SoundCloudTrack:
    """One song of a SoundCloud set, plus its place in it. The same
    shape the tagger, lyrics lookup and YouTube search use (.name,
    .primary_artist, .album_name, .year, .list_position, .cover_url,
    .duration_seconds)."""
    soundcloud_id: str
    title: str
    artists: List[str] = field(default_factory=list)
    album_name: Optional[str] = None
    year: Optional[str] = None
    duration_ms: int = 0           # the full length, even for a preview-only track
    cover_url: Optional[str] = None
    permalink_url: str = ""
    list_position: int = 0
    policy: str = ""               # SoundCloud's: ALLOW, MONETIZE, SNIP (preview only), BLOCK
    genre: str = ""
    disc_number: int = 0           # album songs: 1 of 1
    disc_total: int = 0
    track_total: int = 0

    @property
    def name(self) -> str:
        return self.title

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000 if self.duration_ms else 0.0

    @property
    def preview_only(self) -> bool:
        """SoundCloud only plays a 30-second preview of it (or nothing)."""
        return self.policy in PREVIEW_POLICIES


@dataclass
class SetInfo:
    """A SoundCloud set's own details."""
    path: str                      # config_manager's soundcloud_set: "<user>/sets/<name>[/s-...]"
    name: str
    uploader: str = ""
    artist: str = ""               # the album's artist: its songs' artist, else the uploader
    is_album: bool = False
    set_type: str = ""             # album, ep, single, compilation, or "" for a playlist
    year: Optional[str] = None
    cover_url: Optional[str] = None
    total_tracks: int = 0

    @property
    def primary_artist(self) -> str:
        return self.artist or self.uploader

    @property
    def link(self) -> str:
        return config_manager.soundcloud_link(self.path)

    @property
    def kind(self) -> str:
        return config_manager.KIND_ALBUM if self.is_album else config_manager.KIND_PLAYLIST

    @property
    def folder_name(self) -> str:
        """"Artist - Album" for an album (Michael's album rule); the set's
        own title for a playlist, or for an album whose title already
        starts with "Artist - " (labels often upload it that way). Characters
        Windows forbids become "_"."""
        artist = self.primary_artist
        name = self.name
        if self.is_album and artist and not name.lower().startswith(f"{artist.lower()} - "):
            name = f"{artist} - {name}"
        name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
        return name.strip().rstrip(".") or "SoundCloud"


def _extractor():
    """yt-dlp's SoundCloud extractor, one per thread (it holds the
    anonymous client ID, which yt-dlp looks up once and caches)."""
    ie = getattr(_local, "ie", None)
    if ie is None:
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        ie = ydl.get_info_extractor("Soundcloud")
        ie.initialize()
        _local.ydl, _local.ie = ydl, ie
    return ie


def _api(path: str, **query):
    ie = _extractor()
    url = path if path.startswith("http") else ie._API_V2_BASE + path
    return ie._call_api(url, None, query=query, note=False)


def _year(*dates) -> Optional[str]:
    for d in dates:
        if d and re.match(r"\d{4}", str(d)):
            return str(d)[:4]
    return None


def _big_artwork(url: Optional[str]) -> Optional[str]:
    """SoundCloud artwork links end in "-large" (100 px); "-t500x500" is the
    same picture at 500 px."""
    return url.replace("-large.", "-t500x500.") if url else None


def resolve_short_link(url: str) -> str:
    """Follow an on.soundcloud.com short link to the soundcloud.com link
    it stands for (network). Returns the input unchanged if it isn't a
    short link; raises RuntimeError if it can't be followed."""
    if not config_manager.is_soundcloud_short_link(url):
        return url
    try:
        import requests
        response = requests.get(url.strip() if url.strip().startswith("http") else "https://" + url.strip(),
                                allow_redirects=True, timeout=15)
        return response.url
    except Exception as e:
        raise RuntimeError(f"Couldn't follow the SoundCloud short link ({e})") from e


def _read_set(set_input: str) -> Tuple[dict, str]:
    """(the set's data, its path). Raises RuntimeError with a user-facing
    message if it can't be read."""
    try:
        link = resolve_short_link(set_input)
        path = config_manager.extract_soundcloud_set(link) or (set_input or "").strip()
        data = _api(_extractor()._resolv_url(config_manager.soundcloud_link(path)))
        if not data or data.get("kind") != "playlist":
            raise ValueError("that link isn't a SoundCloud playlist or album")
        canonical = config_manager.extract_soundcloud_set(data.get("permalink_url") or "") or path
        secret = path.split("/")[3] if path.count("/") >= 3 else None
        if secret and not canonical.endswith(secret):
            canonical = f"{canonical}/{secret}"
        return data, canonical
    except Exception as e:
        logger.log_error(f"Failed to read SoundCloud set {set_input}: {e}")
        raise RuntimeError(
            f"Could not read SoundCloud set '{set_input}'. Double check the link is right and the "
            f"playlist or album is public (or a private link with its secret part). ({e})"
        ) from e


def _album_artist(data: dict) -> str:
    """The artist most of the set's songs name (SoundCloud's publisher
    data, in the songs the set's own data includes), or "" -- a label's
    album is uploaded by the label but is by its artist. A compilation
    is by its uploader."""
    if data.get("set_type") == "compilation":
        return ""
    names = [((t.get("publisher_metadata") or {}).get("artist") or "").strip()
             for t in data.get("tracks") or [] if t.get("title")]
    names = [n for n in names if n]
    return max(set(names), key=names.count) if names else ""


def _set_info(data: dict, path: str) -> SetInfo:
    return SetInfo(
        path=path,
        name=data.get("title") or "",
        uploader=((data.get("user") or {}).get("username") or ""),
        artist=_album_artist(data) if data.get("is_album") else "",
        is_album=bool(data.get("is_album")),
        set_type=data.get("set_type") or "",
        year=_year(data.get("release_date"), data.get("display_date"), data.get("created_at")),
        cover_url=_big_artwork(data.get("artwork_url")),
        total_tracks=int(data.get("track_count") or len(data.get("tracks") or [])),
    )


def get_set_info(set_input: str) -> SetInfo:
    """A set's title, uploader, whether it's an album, year, cover and
    track count -- for the Add dialog (and to follow a short link)."""
    data, path = _read_set(set_input)
    return _set_info(data, path)


def get_set_tracks(
    set_input: str,
    fetch_details: bool = True,
    skip_details_for: Optional[Collection[str]] = None,
    details_only_for: Optional[Collection[str]] = None,
) -> List[SoundCloudTrack]:
    """The songs of a public (or secret-linked) SoundCloud set, in its
    order. Every song's details are always read (one call per 50 songs is
    cheap, and a Scan needs the titles of the songs it lists), so the
    details arguments are accepted only to match the other readers. A song
    SoundCloud returns no details for (removed, made private) keeps its
    place but isn't a track (logged)."""
    data, path = _read_set(set_input)
    info = _set_info(data, path)
    listed = [t for t in (data.get("tracks") or []) if t.get("id")]
    if not listed:
        raise RuntimeError(f"SoundCloud set '{path}' has no tracks.")

    full: Dict[int, dict] = {t["id"]: t for t in listed if t.get("title")}
    missing = [t["id"] for t in listed if t["id"] not in full]
    secret = data.get("secret_token")
    for start in range(0, len(missing), _BATCH):
        ids = ",".join(str(i) for i in missing[start:start + _BATCH])
        query = {"ids": ids}
        if secret:
            query["playlistSecretToken"] = secret
        try:
            for t in _api("tracks", **query) or []:
                full[t["id"]] = t
        except Exception as e:
            logger.log_error(f"Couldn't read the details of some songs of SoundCloud set {path}: {e}")
            raise RuntimeError(f"Could not read the songs of SoundCloud set '{path}'. ({e})") from e

    album = info.is_album
    total = len(listed)
    tracks: List[SoundCloudTrack] = []
    for position, item in enumerate(listed, start=1):
        t = full.get(item["id"])
        if not t:
            logger.log_event(f"SoundCloud set {path}: song {position} ({item['id']}) gives no details "
                             f"(removed or private), so it can't be downloaded.")
            continue
        publisher = t.get("publisher_metadata") or {}
        uploader = (t.get("user") or {}).get("username") or ""
        artist = (publisher.get("artist") or "").strip() or uploader
        tracks.append(SoundCloudTrack(
            soundcloud_id=str(t["id"]),
            title=t.get("title") or str(t["id"]),
            artists=[artist] if artist else [],
            album_name=info.name if album else ((publisher.get("album_title") or "").strip() or None),
            year=(info.year if album else None) or _year(t.get("release_date"), t.get("display_date"),
                                                        t.get("created_at")),
            duration_ms=int(t.get("full_duration") or t.get("duration") or 0),
            cover_url=_big_artwork(t.get("artwork_url")) or info.cover_url
            or _big_artwork((t.get("user") or {}).get("avatar_url")),
            permalink_url=t.get("permalink_url") or "",
            list_position=position,
            policy=t.get("policy") or "",
            genre=t.get("genre") or "",
            disc_number=1 if album else 0,
            disc_total=1 if album else 0,
            track_total=total if album else 0,
        ))
    if not tracks:
        raise RuntimeError(f"SoundCloud set '{path}' returned no readable songs.")
    return tracks
