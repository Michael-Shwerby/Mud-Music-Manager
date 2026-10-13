"""
Mud Music Manager - Downloader

Thin wrapper around yt-dlp for downloading a single matched YouTube
video's audio as an m4a file -- called directly, no spotDL.

Deliberately does NOT force a fixed target bitrate by default. yt-dlp's
audio extractor copies the source stream as-is when it's already AAC
(FFmpegExtractAudioPP uses acodec='copy'); any other source is
re-encoded to AAC at ffmpeg's default bitrate. Forcing a fixed bitrate
was the root cause of a real, confirmed audio-quality regression
investigated earlier in this project -- this avoids repeating that.
Note (measured v0.16.44): "bestaudio" on YouTube is nearly always the
Opus stream, so with that source most downloads ARE re-encoded
(~130 kbps, 48 kHz). Since v0.17.2 the default source is YouTube's AAC
stream ("bestaudio[ext=m4a]"), which is copied, not re-encoded.

Download settings (v0.16.44, Checklist item 39; file format v0.16.45):
the file format (.m4a by default; .mp3 / .ogg / .flac / .wav), the source stream, the
conversion bitrate, a speed limit and a pause before each download come
from download_settings.ydl_options() (Settings -> Download quality).
Nothing is asked before a download.

Retry on "download blocked" (v0.16.27, Checklist item 28): measured,
YouTube refuses roughly 1 in 10 audio links with HTTP 403 at random, and
the same video downloads on the next try. yt-dlp doesn't retry a 403
itself (the signed link is what's refused), so download_audio() asks
again from scratch -- a fresh extraction, so a fresh link -- up to
DOWNLOAD_ATTEMPTS times, pausing briefly in between. Any other error
fails at once, as before.

probe_video() (v0.16.38, Checklist item 44) asks yt-dlp for a video's
details without downloading anything, so the Edit page can warn as soon
as an exception link is entered that the video can't be downloaded
(age-restricted, private, removed...).

convert_to_m4a() (v0.16.40, Checklist item 47) turns the user's own audio
file (mp3, FLAC, WAV, Ogg, Opus...) into an m4a with ffmpeg, the same
ffmpeg yt-dlp uses, for importing it as a song. An .m4a is copied as it
is by the caller and never comes here.

Signed-in retry (v0.16.42, Checklist item 48): downloads stay anonymous.
Only when one is refused as age-restricted or by YouTube's "not a bot"
check (youtube_cookies.RETRY_KINDS), and YouTube sign-in is on in
Settings, is it tried once more with the chosen browser's cookies or
cookies.txt (the 403 retry above applies to that pass too). probe_video()
does the same, so the Edit page's link check agrees with what a sync
would do.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import yt_dlp

import download_settings
import logger
import track_failures
import youtube_cookies

DOWNLOAD_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 2.5

# Imported files (v0.16.40): converted to AAC at this bitrate (a high
# quality, widely playable m4a); long files get this long to convert.
IMPORT_AAC_BITRATE = "256k"
CONVERT_TIMEOUT_SECONDS = 600


def sanitize_filename(name: str) -> str:
    """Public name of _sanitize_filename() (v0.16.40), for imports."""
    return _sanitize_filename(name)


def ffmpeg_path() -> Optional[str]:
    """Where ffmpeg is (on PATH, as yt-dlp finds it), or None."""
    return shutil.which("ffmpeg")


def convert_to_m4a(source: Path, dest: Path) -> Optional[str]:
    """Convert the first audio stream of `source` to AAC in an m4a at
    `dest` (overwritten). Tags and pictures of the source are not copied
    (the caller tags the result). Returns None on success, otherwise a
    short reason. Never raises."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return "ffmpeg wasn't found, so this file can't be converted (an .m4a file can still be imported)"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
           "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-c:a", "aac", "-b:a", IMPORT_AAC_BITRATE,
           str(dest)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # no console flash from the GUI
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=CONVERT_TIMEOUT_SECONDS,
                              creationflags=flags)
    except subprocess.TimeoutExpired:
        return f"converting took longer than {CONVERT_TIMEOUT_SECONDS // 60} minutes and was stopped"
    except OSError as e:
        return f"couldn't run ffmpeg: {e}"
    if done.returncode != 0 or not Path(dest).exists():
        detail = (done.stderr or "").strip().splitlines()
        logger.log_error(f"ffmpeg couldn't convert {source}: {(done.stderr or '').strip()}")
        return f"ffmpeg couldn't convert it ({detail[-1] if detail else 'no audio found'})"
    return None


def _sanitize_filename(name: str) -> str:
    """Strip characters that are unsafe in Windows/Mac/Linux filenames.
    Deliberately minimal -- just enough to avoid crashes, not a full
    slugify (keeps spaces, punctuation that's safe)."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _clean_error_message(e: Exception) -> str:
    """yt-dlp's own exception messages are usually already specific and
    human-readable (e.g. "HTTP Error 403: Forbidden") -- just strip a
    leading "ERROR: " prefix if present so it reads cleanly wherever
    it's displayed (console, download log), rather than inventing a
    generic message that would hide the real cause. Terminal colour
    codes are removed too (they showed up in older Sync Logs)."""
    message = track_failures.strip_ansi(str(e)).strip()
    if message.startswith("ERROR: "):
        message = message[len("ERROR: "):]
    return message or type(e).__name__


def _clear_partial_files(output_dir: Path, safe_name: str) -> None:
    """Remove whatever a refused attempt left in the staging folder for
    this track (e.g. an empty .part file), so the next attempt starts
    clean instead of trying to resume a refused link."""
    try:
        for path in output_dir.iterdir():
            if path.is_file() and path.name.startswith(safe_name + "."):
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _wants_signed_in_retry(kind: Optional[str]) -> bool:
    return kind in youtube_cookies.RETRY_KINDS and youtube_cookies.is_on()


def probe_video(video_url: str) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Read a video's details without downloading it. Returns (title,
    failure_reason, failure_kind, signed_in): title set (failure None) if
    yt-dlp could get it ready to download; otherwise the reason and one
    of track_failures' kinds, as download_audio() would report them.
    signed_in is True when the answer came from the signed-in retry
    (v0.16.42). Never raises. A 403 here isn't retried: only the
    download link is refused that way, and probing never fetches it.
    The format follows Settings -> Download quality (v0.16.44), so it
    checks the stream a download would use."""
    ydl_opts = {
        "format": download_settings.format_selector(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        return (info or {}).get("title") or "", None, None, False
    except Exception as e:
        reason = _clean_error_message(e)
        kind = track_failures.classify_download_error(reason)
    if not _wants_signed_in_retry(kind):
        return None, reason, kind, False
    source = youtube_cookies.describe()
    try:
        info = youtube_cookies.run_signed_in(ydl_opts, lambda ydl: ydl.extract_info(video_url, download=False))
    except youtube_cookies.CookieError as e:
        return None, f"{reason} -- couldn't sign in with {source}: {e}", kind, False
    except Exception as e:
        signed_reason = _clean_error_message(e)
        return None, signed_reason, track_failures.classify_download_error(signed_reason), True
    return (info or {}).get("title") or "", None, None, True


def _download_tries(video_url: str, ydl_opts: dict, output_dir: Path, safe_name: str,
                    signed_in: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """Download with the "download blocked" retry: (None, None) on
    success, else (reason, kind). signed_in (v0.16.42) uses the cookies
    from Settings for every try; it raises youtube_cookies.CookieError if
    they can't be loaded."""
    how = " signed in" if signed_in else ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            if signed_in:
                youtube_cookies.run_signed_in(ydl_opts, lambda ydl: ydl.extract_info(video_url, download=True))
            else:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(video_url, download=True)
        except youtube_cookies.CookieError:
            raise
        except Exception as e:
            reason = _clean_error_message(e)
            kind = track_failures.classify_download_error(reason)
            if kind != track_failures.DOWNLOAD_BLOCKED or attempt == DOWNLOAD_ATTEMPTS:
                if attempt > 1:
                    reason = f"{reason} (tried {attempt} times)"
                return reason, kind
            logger.log_event(
                f"Download{how} of {video_url} was refused ({reason}) on try {attempt} of "
                f"{DOWNLOAD_ATTEMPTS} -- trying again with a fresh link"
            )
            _clear_partial_files(output_dir, safe_name)
            time.sleep(RETRY_PAUSE_SECONDS)
            continue
        if attempt > 1:
            logger.log_event(f"Download{how} of {video_url} succeeded on try {attempt} of {DOWNLOAD_ATTEMPTS}")
        return None, None
    return "download failed", track_failures.DOWNLOAD_ERROR  # not reached


def download_audio(
    video_url: str, output_dir: Path, filename_base: str
) -> Tuple[Optional[Path], Optional[str], Optional[str]]:
    """
    Download the given YouTube video's audio as an m4a file into
    output_dir, named after filename_base.

    Returns (file_path, failure_reason, failure_kind). On success,
    file_path is set and the other two are None. On failure, file_path
    is None, failure_reason is a specific, human-readable message (e.g.
    "HTTP Error 403: Forbidden (tried 3 times)") -- not just a generic
    "download failed" -- suitable for showing directly in the console or
    a download log, in addition to being written to logs/error_log.txt,
    and failure_kind is one of track_failures' kinds (download blocked,
    video unavailable, no connection, ...).

    A "download blocked" error (403 / 429) is retried, up to
    DOWNLOAD_ATTEMPTS tries in all; an age-restricted / bot-check refusal
    is tried once more signed in when that is on in Settings (v0.16.42).
    See the module docstring.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_filename(filename_base)
    output_template = str(output_dir / f"{safe_name}.%(ext)s")

    # Read once, so the options and the expected file extension agree even
    # if the setting changes during the download (v0.16.45).
    settings = download_settings.get_setting()
    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # format + postprocessors (the chosen file format's extract, m4a by
        # default; no preferredquality unless a bitrate is chosen -- see
        # module docstring), and optionally ratelimit / sleep_interval,
        # from Settings (v0.16.44).
        **download_settings.ydl_options(settings),
    }

    reason, kind = _download_tries(video_url, ydl_opts, output_dir, safe_name)
    if reason is not None and _wants_signed_in_retry(kind):
        source = youtube_cookies.describe()
        logger.log_event(f"Download of {video_url} was refused ({track_failures.label(kind)}) -- "
                         f"trying once more signed in with {source}")
        _clear_partial_files(output_dir, safe_name)
        try:
            signed_reason, signed_kind = _download_tries(video_url, ydl_opts, output_dir, safe_name,
                                                         signed_in=True)
        except youtube_cookies.CookieError as e:
            reason = f"{reason} -- couldn't sign in with {source}: {e}"
        else:
            if signed_reason is None:
                logger.log_event(f"Download of {video_url} succeeded signed in with {source}")
                reason, kind = None, None
            else:
                reason, kind = f"{signed_reason} (signed in with {source})", signed_kind
    if reason is not None:
        logger.log_error(f"Failed to download {video_url}: {reason}")
        return None, reason, kind

    expected_path = output_dir / f"{safe_name}{download_settings.file_extension(settings)}"
    if expected_path.exists():
        return expected_path, None, None

    reason = "download reported success but the output file was not found afterward"
    logger.log_error(f"Download reported success for {video_url} but the expected "
                      f"output file was not found at {expected_path}")
    return None, reason, track_failures.DOWNLOAD_ERROR
