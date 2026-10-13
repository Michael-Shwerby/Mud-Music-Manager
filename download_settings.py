"""
Mud Music Manager - Download Settings (v0.16.44, Checklist item 39)

App-wide yt-dlp download settings, changed only in Settings -> Download
quality (stored in ui_settings.json under "download_settings"). Nothing
is ever asked before a download (Michael, 2026-10-09): a setting the user
hasn't changed uses the default below, and "Reset to defaults" puts them
back. Until v0.17.2 every default was the app's original behaviour; since
then the source audio default is "aac" (see below).

  - file_format (v0.16.45): "m4a" (default, AAC), "mp3", "ogg" (Vorbis), "flac" or
      "wav". yt-dlp's FFmpegExtractAudio "preferredcodec" (m4a / mp3 /
      vorbis / flac / wav). Every format but m4a is a conversion of
      YouTube's lossy stream, so .flac / .wav are lossless copies of
      that conversion, not better than the source (and much bigger).
  - source: which YouTube audio stream is downloaded.
      "aac" (default since v0.17.2, Michael, 2026-10-11): YouTube's own
        AAC stream ("bestaudio[ext=m4a]", ~128 kbps, 44.1 kHz), copied
        into the .m4a without re-encoding (yt-dlp's extract step uses
        acodec 'copy' for an AAC source). Falls back to the best stream
        (converted) when a video has no AAC stream.
      "best" (the default before v0.17.2): yt-dlp's "bestaudio/best".
        On YouTube that is nearly always the Opus stream, which the m4a
        step re-encodes to AAC (measured on downloaded files: ~130 kbps,
        48 kHz) -- a second lossy encode.
      Because every save writes all five values, a stored "best" from
      before v0.17.2 is switched to "aac" once (_migrate_source_default()).
  - bitrate: the AAC bitrate used when the audio has to be converted
      ("auto" = ffmpeg's default, as before; or 128-320 kbps). yt-dlp
      ignores it when the stream is copied (an AAC source).
  - cover_format (v0.17.3, Checklist item 54; Michael, 2026-10-11):
      the embedded cover art. Not a yt-dlp option: read by
      metadata_tagger._download_cover_art().
      "png" (default): every embedded cover is a PNG. The cover's
        server is first asked for a PNG it renders from its own source
        picture (Apple Music, YT Music, Deezer; Amazon is tried),
        skipping the server's JPEG compression. Otherwise (Spotify,
        SoundCloud, YouTube, or no real PNG came back) the usual cover
        is downloaded and converted to PNG with ffmpeg (v0.17.4,
        Michael, 2026-10-11): lossless from that JPEG, so it keeps the
        JPEG's quality exactly, no better. If ffmpeg is missing or fails,
        the JPEG is embedded as it is (logged).
      "jpeg": the picture each service sends by default (the behaviour
        before v0.17.3); smaller files.
      The image's bytes are always embedded untouched, so a cover never
      degrades when a song file is copied.
  - speed_limit: yt-dlp "ratelimit" per download ("off" = no limit).
  - pause: a random wait before each download (yt-dlp sleep_interval /
      max_sleep_interval; "off" = none). A candidate fix in the 403
      investigation (Checklist, Open Investigation, item 2).

The file format was added in v0.16.45 at Michael's request: tagging
(metadata_tagger), the library diff (library_sync), Check Integrity and
the Edit page read every one of these formats (design doc §3.1, §3.3).
Changing a setting never touches songs already downloaded; it applies to
the next download, so a folder can hold files of several formats.

Read by the engine (downloader.py), so syncs from the terminal menu use
the same settings. No Qt.
"""

from typing import Optional

import logger
import ui_settings

SETTING_KEY = "download_settings"

# Each setting: [(value, label, description)], the default first.
FORMATS = [
    ("m4a", ".m4a (AAC)", "The app's original format. Plays almost everywhere."),
    ("mp3", ".mp3", "Converted to MP3. Plays everywhere."),
    ("ogg", ".ogg (Vorbis)", "Converted to Ogg Vorbis. Some players (e.g. iTunes) can't play it."),
    ("flac", ".flac (lossless)", "A lossless copy of the converted audio. About 5 times bigger, but it "
                                 "can't sound better than YouTube's stream."),
    ("wav", ".wav (uncompressed)", "Uncompressed audio. About 10 times bigger, but it can't sound better "
                                   "than YouTube's stream. Windows Explorer doesn't show its tags."),
]
# file_format -> (yt-dlp preferredcodec, file extension)
_CODECS = {"m4a": ("m4a", ".m4a"), "mp3": ("mp3", ".mp3"), "ogg": ("vorbis", ".ogg"),
           "flac": ("flac", ".flac"), "wav": ("wav", ".wav")}
LOSSLESS_FORMATS = {"flac", "wav"}

SOURCES = [
    ("aac", "AAC, no conversion",
     "YouTube's own AAC stream (about 128 kbps), copied into the .m4a without re-encoding. "
     "If a video has no AAC stream, the best available one is used."),
    ("best", "Best available",
     "YouTube's best audio stream (usually Opus), converted to AAC for the .m4a file."),
]
COVER_FORMATS = [
    ("png", "PNG",
     "Every cover is a PNG. Apple Music, YT Music and Deezer send one made from their own picture; "
     "other covers (Spotify, SoundCloud, YouTube) are converted to PNG losslessly."),
    ("jpeg", "JPEG",
     "Each service's usual JPEG cover. Smaller files (a PNG cover adds about 0.2–1 MB per song)."),
]
BITRATES = [
    ("auto", "Automatic", "ffmpeg's default bitrate (about 130 kbps for AAC, 128 kbps for MP3)."),
    ("128", "128 kbps", ""),
    ("160", "160 kbps", ""),
    ("192", "192 kbps", ""),
    ("256", "256 kbps", ""),
    ("320", "320 kbps", ""),
]
SPEED_LIMITS = [
    ("off", "No limit", ""),
    ("1", "1 MB/s", ""),
    ("2", "2 MB/s", ""),
    ("5", "5 MB/s", ""),
    ("10", "10 MB/s", ""),
]
PAUSES = [
    ("off", "None", ""),
    ("1-3", "1–3 seconds", ""),
    ("2-5", "2–5 seconds", ""),
    ("5-10", "5–10 seconds", ""),
]

OPTIONS = {
    "file_format": FORMATS,
    "source": SOURCES,
    "bitrate": BITRATES,
    "cover_format": COVER_FORMATS,
    "speed_limit": SPEED_LIMITS,
    "pause": PAUSES,
}
NAMES = {
    "file_format": "File format",
    "source": "Source audio",
    "bitrate": "Conversion bitrate",
    "cover_format": "Cover art",
    "speed_limit": "Speed limit",
    "pause": "Pause before each download",
}
DEFAULTS = {key: choices[0][0] for key, choices in OPTIONS.items()}

# v0.17.2: set in ui_settings.json once a stored "best" source (the old
# default) has been switched to "aac", so a later pick of "best" stays.
SOURCE_MIGRATED_KEY = "download_source_default_v0172"
_source_migrated = False


def _migrate_source_default() -> None:
    """Once per settings file: a stored source "best" becomes "aac" (the
    new default). Before v0.17.2 every save wrote all five values, so a
    stored "best" almost always meant "the default at the time"."""
    global _source_migrated
    if _source_migrated:
        return
    _source_migrated = True
    if ui_settings.get(SOURCE_MIGRATED_KEY):
        return
    raw = ui_settings.get(SETTING_KEY)
    if isinstance(raw, dict) and raw.get("source") == "best":
        ui_settings.set_value(SETTING_KEY, {**raw, "source": "aac"})
        logger.log_event("Download settings: Source audio switched from Best available to the new default, "
                         "AAC, no conversion (v0.17.2)")
    ui_settings.set_value(SOURCE_MIGRATED_KEY, True)


def get_setting() -> dict:
    """{file_format, source, bitrate, cover_format, speed_limit, pause},
    with bad / missing values replaced by the defaults (a settings file
    from before v0.17.3 has no cover_format, so it gets "png")."""
    _migrate_source_default()
    raw = ui_settings.get(SETTING_KEY)
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, choices in OPTIONS.items():
        value = raw.get(key)
        out[key] = value if value in {v for v, _, _ in choices} else DEFAULTS[key]
    return out


def set_setting(**change) -> dict:
    """Change any of the settings; returns the new setting. Unknown keys
    or values are ignored."""
    current = get_setting()
    for key, value in change.items():
        if key in OPTIONS and value in {v for v, _, _ in OPTIONS[key]}:
            current[key] = value
    ui_settings.set_value(SETTING_KEY, current)
    logger.log_event(f"Download settings set to: {describe(current)}")
    return current


def reset() -> dict:
    """Back to the defaults (since v0.17.2 the source is "aac")."""
    ui_settings.set_value(SETTING_KEY, dict(DEFAULTS))
    logger.log_event("Download settings reset to the defaults")
    return dict(DEFAULTS)


def changed_keys(setting: Optional[dict] = None) -> list:
    """The settings that differ from the defaults, in display order."""
    s = setting or get_setting()
    return [key for key in OPTIONS if s[key] != DEFAULTS[key]]


def is_default(setting: Optional[dict] = None) -> bool:
    return not changed_keys(setting)


def label(key: str, value: str) -> str:
    return next((lab for v, lab, _ in OPTIONS[key] if v == value), value)


def description(key: str, value: str) -> str:
    return next((d for v, _, d in OPTIONS[key] if v == value), "")


def describe(setting: Optional[dict] = None) -> str:
    """e.g. "Source audio: Best available · Conversion bitrate: 256 kbps ·
    Speed limit: No limit · Pause before each download: None"."""
    s = setting or get_setting()
    return " · ".join(f"{NAMES[key]}: {label(key, s[key])}" for key in OPTIONS)


def format_selector(setting: Optional[dict] = None) -> str:
    """yt-dlp's "format" for the chosen source (also used by the Edit
    page's link check, so it checks the stream a download would use)."""
    s = setting or get_setting()
    if s["source"] == "aac":
        return "bestaudio[ext=m4a]/bestaudio/best"
    return "bestaudio/best"


def file_extension(setting: Optional[dict] = None) -> str:
    """The extension a download gets with these settings, e.g. ".mp3"."""
    s = setting or get_setting()
    return _CODECS[s["file_format"]][1]


def bitrate_applies(setting: Optional[dict] = None) -> bool:
    """False for .flac / .wav, which have no bitrate to choose."""
    s = setting or get_setting()
    return s["file_format"] not in LOSSLESS_FORMATS


def ydl_options(setting: Optional[dict] = None) -> dict:
    """The yt-dlp options these settings control, for a download. With
    the defaults (v0.17.2): "bestaudio[ext=m4a]/bestaudio/best" and an
    m4a extract with no preferredquality, so an AAC stream is copied.
    Source "best" gives exactly what downloader.download_audio() used
    before v0.16.44: "bestaudio/best" and the same extract."""
    s = setting or get_setting()
    extract = {"key": "FFmpegExtractAudio", "preferredcodec": _CODECS[s["file_format"]][0]}
    if s["bitrate"] != "auto" and bitrate_applies(s):
        extract["preferredquality"] = s["bitrate"]  # only used when converting
    opts = {"format": format_selector(s), "postprocessors": [extract]}
    if s["speed_limit"] != "off":
        opts["ratelimit"] = int(float(s["speed_limit"]) * 1024 * 1024)
    if s["pause"] != "off":
        low, high = (int(x) for x in s["pause"].split("-"))
        opts["sleep_interval"] = low
        opts["max_sleep_interval"] = high
    return opts
