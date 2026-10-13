"""
Mud Music Manager - Artists and new releases (v0.16.50)

Checklist item 41b (artist discography) and 41c (watching an artist).

An artist link is not a source a playlist entry can track. Adding one
(the discography picker in the GUI, Add in the terminal menu) adds the
releases the user ticks -- the artist's albums and singles & EPs (Michael's
choice) -- as ordinary album entries (config_manager, kind "album") that
carry the artist's ID in "artist_id", and remembers the artist in
playlists.json's "artists" list:

    {"spotify_artist_id": "...", "name": "Daft Punk", "watch": true,
     "known_releases": ["<album id>", ...], "last_checked": <unix time>}

known_releases is every release the user has been shown: the ones added
AND the ones left unticked. That is what makes a release "new": it was
released (or first listed) after the artist was last looked at.

Watching (41c, Michael's choices, 2026-10-09): a watched artist ("watch"
true, the picker's checkbox, ticked by default) is checked for new
releases with every Scan -- the launch scan, Scan and Sync Now (for the
artists whose albums are in the run) and the terminal menu's Sync / Scan.
A new release is added and downloaded automatically: add_new_releases()
adds it as an album, and the run that checked it syncs (or scans) it
straight away. A release the user removes later stays in known_releases,
so it is never added again by itself.

No Qt here: the terminal menu uses this module as it is, and the GUI
reads the discography on a worker thread (read_artist()) and applies the
result on the GUI thread (add_new_releases() and the rest write
playlists.json).

YT Music artists (v0.16.51, Checklist item 42a): a YT Music artist link
works the same way. Its entry is {"platform": "ytmusic",
"ytmusic_artist_id": "UC...", ...} (config_manager.new_artist()), its
releases are YT Music albums (their browse IDs), and everything above
applies unchanged. Artist IDs are passed around bare: the two platforms'
IDs can't be confused (config_manager.id_platform()).
"""

import time
from typing import Dict, Iterable, List, Optional, Tuple

import acquisition_engine
import config_manager
import logger
from spotify_reader import ArtistInfo, Release

# Characters Windows doesn't allow in a folder name.
_FORBIDDEN = '<>:"/\\|?*'


def read_artist(artist_input: str) -> ArtistInfo:
    """The artist's name, picture and releases (network). Raises
    RuntimeError if it can't be read."""
    return acquisition_engine.get_artist_discography(artist_input)


def artist_link(artist_id: str) -> str:
    platform = config_manager.id_platform(artist_id)
    if platform == config_manager.PLATFORM_YTMUSIC:  # v0.16.51
        return f"https://music.youtube.com/channel/{artist_id}"
    if platform == config_manager.PLATFORM_APPLE_MUSIC:  # v0.16.53: "us/artist/5468295"
        country, _, rest = artist_id.partition("/")
        return f"https://music.apple.com/{country}/artist/{rest.split('/', 1)[-1]}"
    return f"https://open.spotify.com/artist/{artist_id}"


def platform_name(artist_id: str) -> str:
    """"Spotify" or "YT Music" (v0.16.51), for messages about the artist."""
    return config_manager.platform_name(config_manager.id_platform(artist_id))


def _clean(name: str) -> str:
    name = "".join("_" if c in _FORBIDDEN or ord(c) < 32 else c for c in name)
    return name.strip().rstrip(".") or "Album"


def tracked_albums(config) -> Dict[str, dict]:
    """{album id: entry} for every album tracked."""
    return {config_manager.get_playlist_source_id(e): e
            for e in config_manager.list_playlists(config) if config_manager.is_album(e)}


def plan_folder_names(config, artist_name: str, releases: Iterable[Release],
                      taken: Iterable[str] = ()) -> Dict[str, str]:
    """{album id: folder name} for releases about to be added: "Artist -
    Release" (Michael's choice for albums). If that name is already used
    (by a tracked entry, by `taken`, or by an earlier release here), the
    release type is added -- "Daft Punk - Get Lucky (Single)" -- then a
    number."""
    used = {e["folder_name"].lower() for e in config_manager.list_playlists(config)}
    used |= {n.lower() for n in taken}
    names = {}
    for r in releases:
        base = _clean(f"{artist_name} - {r.name}" if artist_name else r.name)
        name = base
        if name.lower() in used:
            name = _clean(f"{base} ({r.type_text})")
        n = 2
        while name.lower() in used:
            name = _clean(f"{base} ({r.type_text}) ({n})")
            n += 1
        used.add(name.lower())
        names[r.album_id] = name
    return names


def remember_artist(config, info: ArtistInfo, watch: bool, shown: Iterable[str]) -> dict:
    """Add the artist (or update it, if it is already tracked) after the
    picker: its name, whether it is watched, and every release shown in
    the picker added to known_releases. Albums already tracked that are
    among its releases become its albums (if they belong to no other
    tracked artist). Saves playlists.json; returns the artist dict."""
    artist = config_manager.find_artist(config, info.artist_id)
    if artist is None:
        artist = config_manager.new_artist(config_manager.id_platform(info.artist_id), info.artist_id,
                                           name=info.name, watch=bool(watch),
                                           known_releases=[], last_checked=time.time())
        config.setdefault("artists", []).append(artist)
        logger.log_event(f"Added artist '{info.name}' ({info.artist_id}), watching: {bool(watch)}")
    else:
        artist["name"] = info.name or artist.get("name", "")
        artist["watch"] = bool(watch)
        artist["last_checked"] = time.time()
    known = artist.setdefault("known_releases", [])
    for album_id in shown:
        if album_id not in known:
            known.append(album_id)
    release_ids = {r.album_id for r in info.releases}
    for album_id, entry in tracked_albums(config).items():
        if album_id in release_ids and not config_manager.entry_artist_id(config, entry):
            entry["artist_id"] = info.artist_id
    config_manager.group_playlists(config)
    config_manager.save_config(config)
    return artist


def new_releases(config, artist: dict, info: ArtistInfo) -> List[Release]:
    """The artist's releases never shown before and not tracked already."""
    known = set(artist.get("known_releases") or [])
    tracked = tracked_albums(config)
    return [r for r in info.releases if r.album_id not in known and r.album_id not in tracked]


def add_new_releases(config, artist_id: str, info: ArtistInfo) -> List[dict]:
    """41c: add the watched artist's new releases as albums (each "Artist -
    Release", its folder created) and remember them, so they are added
    once. Saves playlists.json. Returns the entries added (empty if the
    artist isn't tracked or isn't watched, or nothing is new). An entry
    add_playlist() refuses is logged and remembered, so it isn't retried
    every Scan."""
    artist = config_manager.find_artist(config, artist_id)
    if artist is None or not artist.get("watch"):
        return []
    fresh = new_releases(config, artist, info)
    artist["last_checked"] = time.time()
    if info.name:
        artist["name"] = info.name
    added = []
    names = plan_folder_names(config, artist["name"], fresh)
    for release in fresh:
        artist.setdefault("known_releases", []).append(release.album_id)
        try:
            entry = config_manager.add_playlist(config, release.link, names[release.album_id],
                                                artist_id=artist_id)
        except ValueError as e:
            logger.log_error(f"Couldn't add new release '{release.name}' of '{artist['name']}': {e}")
            continue
        logger.log_event(f"New release of '{artist['name']}' added: '{entry['folder_name']}' "
                         f"({release.type_text}, {release.album_id})")
        added.append(entry)
    config_manager.save_config(config)
    return added


def check_artist(config, artist_id: str) -> Tuple[List[dict], Optional[str]]:
    """Read a watched artist and add its new releases, in one go (the
    terminal menu). Returns (entries added, error text or None)."""
    try:
        info = read_artist(artist_id)
    except RuntimeError as e:
        return [], str(e)
    return add_new_releases(config, artist_id, info), None


def watched_artist_ids(config, entries: Optional[Iterable[dict]] = None) -> List[str]:
    """The watched artists to check: all of them, or (with entries) those
    whose albums are among the entries, in the artists' order."""
    wanted = None
    if entries is not None:
        wanted = {config_manager.entry_artist_id(config, e) for e in entries} - {None}
    ids = [config_manager.artist_id_of(a) for a in config_manager.list_artists(config) if a.get("watch")]
    return [i for i in ids if wanted is None or i in wanted]


def artist_albums(config, artist_id: str) -> List[dict]:
    """The tracked albums of this artist, in list order."""
    return [e for e in config_manager.list_playlists(config)
            if config_manager.entry_artist_id(config, e) == artist_id]


def set_watch(config, artist_id: str, watch: bool) -> None:
    artist = config_manager.find_artist(config, artist_id)
    if artist is None:
        raise ValueError("That artist isn't tracked.")
    artist["watch"] = bool(watch)
    config_manager.save_config(config)
    logger.log_event(f"Artist '{artist.get('name')}': watching for new releases {'on' if watch else 'off'}")


def remove_artist(config, artist_id: str, remove_albums: bool) -> List[dict]:
    """Stop tracking the artist (and stop watching it). With remove_albums
    its albums are removed from the list too; otherwise they stay, as
    albums on their own. Files are never deleted. Returns the album
    entries removed."""
    artist = config_manager.find_artist(config, artist_id)
    if artist is None:
        raise ValueError("That artist isn't tracked.")
    albums = artist_albums(config, artist_id)
    removed = []
    if remove_albums:
        ids = {id(e) for e in albums}
        config["playlists"] = [e for e in config["playlists"] if id(e) not in ids]
        removed = albums
    else:
        for e in albums:
            e.pop("artist_id", None)
    config["artists"] = [a for a in config.get("artists", []) if a is not artist]
    config_manager.group_playlists(config)
    config_manager.save_config(config)
    logger.log_event(f"Stopped tracking artist '{artist.get('name')}'"
                     + (f" and its {len(removed)} album(s) (files kept)" if remove_albums else ""))
    return removed
