"""
Mud Music Manager - Playlist files (v0.24.0, Checklist item 64)

Writes a `<playlist name>.m3u8` file inside each playlist's folder, listing
its songs in the playlist's own order (Michael's choice, 2026-10-13: inside
each playlist folder), so other players -- VLC, foobar2000, MusicBee, Plex,
Jellyfin, Poweramp... -- show the real playlist rather than the files
sorted by name.

The order comes from the songs' own tags: a Sync renumbers every file's
Track # to its current place in the live playlist (and, for an album,
Disc #), so after a Sync, Disc # then Track # is the playlist's order. Only
song files with an identity tag (the files the diff manages) are listed;
songs not downloaded, unavailable items and untagged files are left out.

The file is extended M3U in UTF-8 (`.m3u8`): `#EXTM3U`, `#PLAYLIST:<name>`,
then per song `#EXTINF:<seconds>,<Artist> - <Title>` and its file name
(relative, so the folder can be moved or copied as it is). Every file this
module writes carries MARKER on its second line. That is how it knows a
`.m3u8` is its own: it replaces or removes only files with the marker, so a
playlist file the user made is never touched.

When it's written (library_sync): at the end of every completed Sync, and
after an exception fix or an own-file import. The content is compared
first, so an unchanged playlist leaves the file (and its date) alone. A
renamed playlist's old file is removed at its next Sync, when the file
with the new name is written. Settings -> Playlist files (ui_settings
"playlist_files", default On); Off removes the app's files at each
playlist's next Sync. The `.m3u8` is not a song file
(metadata_tagger.audio_files() never lists it), so the diff, Scan and Check
Integrity ignore it.

Never raises: a playlist file that can't be written is logged and the
Sync carries on. No Qt, so the terminal menu's syncs write it too.
"""

import os
from pathlib import Path
from typing import List, Optional

import logger
import metadata_tagger
import ui_settings

EXTENSION = ".m3u8"
SETTING_KEY = "playlist_files"
MARKER = "# Written by Mud Music Manager; rewritten on every Sync, so changes made here are lost."

# Characters Windows doesn't allow in a file name (a folder name never holds
# them, but a Connect Existing playlist's display name might).
_FORBIDDEN = '<>:"/\\|?*'


def enabled() -> bool:
    """Settings -> Playlist files (default On)."""
    return ui_settings.get(SETTING_KEY) is not False


def file_name(name: str) -> str:
    safe = "".join("_" if c in _FORBIDDEN or ord(c) < 32 else c for c in name).strip().rstrip(".")
    return (safe or "Playlist") + EXTENSION


def _is_ours(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            head = [f.readline().rstrip("\r\n") for _ in range(3)]
    except OSError:
        return False
    return MARKER in head


def _our_files(folder: Path) -> List[Path]:
    try:
        candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == EXTENSION]
    except OSError:
        return []
    return [p for p in candidates if _is_ours(p)]


def _entry_line(path: Path, tags) -> str:
    artist, title = (tags.artist or "").strip(), (tags.title or "").strip()
    shown = f"{artist} - {title}" if artist and title else (title or path.stem)
    seconds = int(round(tags.length)) if tags.length else -1
    return f"#EXTINF:{seconds},{shown}"


def build(folder: Path, name: str) -> str:
    """The playlist file's text for the folder as it is now."""
    songs = []
    for path in metadata_tagger.audio_files(folder):
        tags = metadata_tagger.read_tags(path)
        if tags is None or not tags.identity:
            continue
        songs.append(((tags.disc_number or 0, tags.track_number or 10 ** 6, path.name.lower()), path, tags))
    songs.sort(key=lambda s: s[0])
    lines = ["#EXTM3U", MARKER, f"#PLAYLIST:{name}"]
    for _, path, tags in songs:
        lines += [_entry_line(path, tags), path.name]
    return "\n".join(lines) + "\n"


def update(folder, name: str) -> Optional[Path]:
    """Write (or, with the setting Off, remove) the playlist file of one
    playlist folder. Returns the file written, or None. Never raises."""
    folder = Path(folder)
    try:
        if not folder.is_dir():
            return None
        ours = _our_files(folder)
        if not enabled():
            for p in ours:
                p.unlink()
                logger.log_event(f"Removed playlist file '{p.name}' from '{folder.name}' (Playlist files is Off).")
            return None

        target = folder / file_name(name)
        if target.exists() and target not in ours:
            # A playlist file of the user's own with this name: leave it alone.
            logger.log_event(f"Didn't write '{target.name}' in '{folder.name}': a playlist file with that name "
                             "is already there and wasn't written by the app.")
            return None
        text = build(folder, name)
        try:
            current = target.read_text(encoding="utf-8") if target.exists() else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current != text:
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            logger.log_event(f"Wrote playlist file '{target.name}' in '{folder.name}' "
                             f"({text.count('#EXTINF:')} songs).")
        for p in ours:  # e.g. the old name's file after a rename
            if p.resolve() != target.resolve():
                p.unlink()
                logger.log_event(f"Removed old playlist file '{p.name}' from '{folder.name}'.")
        return target
    except Exception as e:  # never stop a sync over its playlist file
        logger.log_error(f"Couldn't update the playlist file in '{folder}': {e}")
        return None
