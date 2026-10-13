"""
Mud Music Manager - YouTube Matcher

Finds the best YouTube match for a Spotify track, using the tiered
strategy Michael and Claude designed together:

  Tier 1 - YouTube Music, ATV ("Audio Track Video" -- official audio-only
           upload, e.g. an artist's "- Topic" channel). Best quality,
           tried first.
  Tier 2 - Plain YouTube search. Tried if Tier 1 finds nothing, since a
           plain-YouTube "Official Audio"/Topic-channel match is
           considered more trustworthy than a YouTube Music "Official
           Music Video" (OMV audio can carry video-specific mixing/
           intros that aren't wanted here).
  Tier 3 - YouTube Music, OMV (Official Music Video). Last resort before
           giving up.
  Tier 4 - No match found -> caller should hand this off to the
           Exceptions system rather than guess.

Each tier is a separate, independently callable function, specifically so
the order/inclusion of tiers can be changed later without restructuring
this module -- Michael has said he expects to want to adjust this.

Uses ytmusicapi (YouTube Music search) and yt-dlp (plain YouTube search),
both directly -- no spotDL. Neither requires login or an API key.

Confirmed "no match" (v0.16.27, Checklist item 28): a search that raises
an error (no connection, YouTube error) used to be treated as an empty
result, so a dropped connection could be reported as "no results at
all". Now each search is retried once, and an error that remains is
reported as its own failure (search failed / no connection), never as no
match. A lower tier is only accepted when every search feeding the
higher tiers worked -- otherwise a worse match could be picked only
because the better one's search failed. find_best_match() reports
"no match" only when every search ran normally.

Prefer explicit versions (v0.17.7, Checklist item 58; Michael's choice):
a Settings switch, off by default (ui_settings "prefer_explicit"). A
song's explicit and clean versions usually share the same title and
length, so before this the pick between them came down to search order.
On, within each tier the explicit version of the same song wins: YouTube
Music's own explicit badge ("isExplicit", only given on "song" results,
i.e. the ATV pool), or "Explicit" / "Uncensored" / "Dirty" in the title;
a title marked "Clean" / "Radio Edit" / "Censored" / "Edited" is avoided.
Only versions of the tier's usual pick are considered: the same title once
version / upload words are left out (_core_title()), and the same artist
(YouTube Music) or the same kind of channel, Topic or not (plain YouTube).
So a different song, a cover or a reupload is never taken because it is
explicit. The tiers, their order and the ±5 s length check are unchanged,
and so is every pick when the switch is off.
"""

import difflib
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ytmusicapi import YTMusic
import yt_dlp

import logger
import track_failures
import ui_settings
from spotify_reader import Track

DURATION_TOLERANCE_SECONDS = 5

# v0.17.7 (Prefer explicit versions): title words that mark a version, and
# words left out when deciding two titles name the same song (_core_title()).
_EXPLICIT_WORDS = ("explicit", "uncensored", "dirty")
_CLEAN_WORDS = ("clean", "radio edit", "censored", "edited", "radio version")
_SAME_SONG_IGNORED = ("explicit version", "clean version", "radio version", "radio edit", "official music video",
                      "official audio", "official video", "music video", "lyric video", "high quality", "explicit",
                      "uncensored", "dirty", "clean", "censored", "edited", "lyrics", "audio", "official", "hq")

# A search that errors is tried once more after this pause (v0.16.27).
SEARCH_ATTEMPTS = 2
SEARCH_RETRY_PAUSE_SECONDS = 2.0

_ytmusic: Optional[YTMusic] = None


@dataclass
class MatchResult:
    video_id: str
    title: str
    tier: str  # "ytmusic_atv" | "youtube" | "ytmusic_omv" | "exception"
    url: str


def _get_ytmusic() -> YTMusic:
    global _ytmusic
    if _ytmusic is None:
        # No language= override here -- this is the fix for spotDL's bug
        # of hardcoding the search client to German. Default is English;
        # change this if Michael's actual content/locale differs.
        #
        # location="US": without this, the request has no country/region
        # context at all ("determined by the server" per ytmusicapi's own
        # docs) -- which is NOT the same as the server correctly inferring
        # it from the requester's IP. Explicitly setting this is a real,
        # testable fix for search results differing from what Michael sees
        # searching the actual YouTube Music site himself, even from the
        # same machine/IP. Change this if Michael's actual region differs.
        _ytmusic = YTMusic(location="US")
    return _ytmusic


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _text_similarity(track: Track, candidate_title: str, candidate_artist: str = "") -> float:
    track_text = _normalize(f"{track.primary_artist} {track.name}")
    candidate_text = _normalize(f"{candidate_artist} {candidate_title}")
    return difflib.SequenceMatcher(None, track_text, candidate_text).ratio()


def _duration_matches(track: Track, candidate_seconds: Optional[float]) -> bool:
    """True if within tolerance, or if we simply don't have a duration
    to compare (some plain-YouTube search results don't expose one) --
    in that case we don't hard-reject, we just can't use duration as a
    signal for this candidate."""
    if candidate_seconds is None:
        return True
    return abs(track.duration_seconds - candidate_seconds) <= DURATION_TOLERANCE_SECONDS


def _search_query(track: Track) -> str:
    # "Song Name Artist Name", per Michael's spec.
    return f"{track.name} {track.primary_artist}"


def prefer_explicit() -> bool:
    """Settings -> Prefer explicit versions (v0.17.7), read at each search
    so a change applies to the next song without a restart."""
    return ui_settings.get("prefer_explicit") is True


def _has_word(text_norm: str, word: str) -> bool:
    return f" {word} " in f" {text_norm} "


def _explicit_rank(track, candidate: dict, title: str) -> int:
    """1 = explicit (YouTube Music's badge, or an explicit word in the
    title), -1 = marked clean in the title, 0 = can't tell. A word that is
    part of the song's own name (e.g. a song called "Clean") isn't a mark."""
    if candidate.get("isExplicit") is True:
        return 1
    title_norm = _normalize(title or "")
    own_name = _normalize(track.name or "")
    if any(_has_word(title_norm, w) and not _has_word(own_name, w) for w in _EXPLICIT_WORDS):
        return 1
    if any(_has_word(title_norm, w) and not _has_word(own_name, w) for w in _CLEAN_WORDS):
        return -1
    return 0


def _core_title(title: str) -> str:
    """The title without version / upload words ("HUMBLE. (Explicit)" and
    "HUMBLE. [Official Audio]" both give "humble")."""
    text = f" {_normalize(title or '')} "
    for words in _SAME_SONG_IGNORED:
        text = text.replace(f" {words} ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _explicit_pick(track, candidates: List[dict], key, title_of, same_song) -> dict:
    """The candidate key() ranks highest, as always -- or, with Prefer
    explicit versions on, the most explicit one among that candidate and
    the others that are the same song (same_song(c, best): same core title,
    plus the same artist where the results say), ties by key(). A different
    song is never taken because it is explicit. Logs when the setting
    changed the pick."""
    best = max(candidates, key=key)
    if not prefer_explicit():
        return best
    same = [c for c in candidates if c is best or same_song(c, best)]
    chosen = max(same, key=lambda c: (_explicit_rank(track, c, title_of(c)), key(c)))
    if chosen is not best:
        logger.log_event(
            f"Prefer explicit versions: '{track.primary_artist} - {track.name}' -> "
            f"'{title_of(chosen)}' ({chosen.get('videoId') or chosen.get('id')}) instead of "
            f"'{title_of(best)}' ({best.get('videoId') or best.get('id')})"
        )
    return chosen


def _search_ytmusic_category(track: Track, filter: str, limit: int = 20) -> Tuple[List[dict], Optional[str]]:
    """One dedicated, single-category search call (filter="songs" or
    "videos"). Confirmed via live testing that this is necessary --
    YTMusic's blended/mixed search (no filter) only returns a shallow
    preview per category and can truncate before reaching a track's
    correct match, even when a dedicated single-category search finds
    it immediately as the top result.

    Returns (results, error): error is None when the search worked
    (even with no results), else the error text after SEARCH_ATTEMPTS
    tries."""
    query = _search_query(track)
    error = None
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        try:
            results = _get_ytmusic().search(query, filter=filter, limit=limit, ignore_spelling=True)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.log_error(
                f"YouTube Music search failed for '{query}' (filter={filter}, try {attempt} of "
                f"{SEARCH_ATTEMPTS}): {error}"
            )
            if attempt < SEARCH_ATTEMPTS:
                time.sleep(SEARCH_RETRY_PAUSE_SECONDS)
            continue
        return [r for r in (results or []) if r.get("videoId") and r.get("resultType") in ("song", "video")], None

    return [], error


def _search_ytmusic_candidates(track: Track, limit: int = 20) -> Tuple[List[dict], Optional[str]]:
    """Combined candidate pool for Tiers 1 and 3: a dedicated "songs"
    search (where ATV results live) merged with a dedicated "videos"
    search (where OMV results live), deduplicated by video ID. Both
    calls are made once per track and the merged result is reused for
    both tiers, so this is still only two searches per track, not
    per-tier. Returns (candidates, error): error is set if either search
    failed."""
    songs, songs_error = _search_ytmusic_category(track, filter="songs", limit=limit)
    videos, videos_error = _search_ytmusic_category(track, filter="videos", limit=limit)

    seen_ids = set()
    merged = []
    for candidate in songs + videos:
        video_id = candidate.get("videoId")
        if video_id and video_id not in seen_ids:
            seen_ids.add(video_id)
            merged.append(candidate)

    return merged, songs_error or videos_error


def _best_by_video_type(track: Track, candidates: List[dict], video_type: str) -> Optional[MatchResult]:
    matching_type = [c for c in candidates if c.get("videoType") == video_type]
    duration_ok = [
        c for c in matching_type
        if _duration_matches(track, c.get("duration_seconds"))
    ]
    if not duration_ok:
        return None

    def artist_name(c):
        artists = c.get("artists") or []
        return artists[0].get("name", "") if artists else ""

    best = _explicit_pick(
        track, duration_ok,
        lambda c: _text_similarity(track, c.get("title", ""), artist_name(c)),
        lambda c: c.get("title", ""),
        lambda c, b: (_normalize(artist_name(c)) == _normalize(artist_name(b))
                      and _core_title(c.get("title", "")) == _core_title(b.get("title", ""))),
    )
    tier_label = "ytmusic_atv" if video_type == "MUSIC_VIDEO_TYPE_ATV" else "ytmusic_omv"
    return MatchResult(
        video_id=best["videoId"],
        title=best.get("title", ""),
        tier=tier_label,
        url=f"https://www.youtube.com/watch?v={best['videoId']}",
    )


def try_ytmusic_atv(track: Track, candidates: List[dict]) -> Optional[MatchResult]:
    """Tier 1."""
    return _best_by_video_type(track, candidates, "MUSIC_VIDEO_TYPE_ATV")


def try_ytmusic_omv(track: Track, candidates: List[dict]) -> Optional[MatchResult]:
    """Tier 3."""
    return _best_by_video_type(track, candidates, "MUSIC_VIDEO_TYPE_OMV")


def _search_youtube_plain(track: Track, limit: int = 10) -> Tuple[List[dict], Optional[str]]:
    """Plain YouTube search via yt-dlp, no download. Returns yt-dlp's
    flat search entries (title, channel/uploader, duration if available,
    id) -- note plain YouTube search does not expose anything like
    videoType, so this tier relies on channel-name and title-text
    signals instead. Returns (entries, error), like
    _search_ytmusic_category()."""
    query = _search_query(track)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    error = None
    for attempt in range(1, SEARCH_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            return (info or {}).get("entries", []) or [], None
        except Exception as e:
            error = track_failures.strip_ansi(str(e)).strip() or type(e).__name__
            logger.log_error(f"Plain YouTube search failed for '{query}' (try {attempt} of {SEARCH_ATTEMPTS}): {error}")
            if attempt < SEARCH_ATTEMPTS:
                time.sleep(SEARCH_RETRY_PAUSE_SECONDS)
    return [], error


def try_youtube_plain(track: Track, candidates: List[dict]) -> Optional[MatchResult]:
    """Tier 2. Scores candidates by: duration match (soft, see
    _duration_matches), then channel name containing "Topic" and
    matching the artist, then "Official Audio"/"HQ"/"High Quality" in
    the title, then general text similarity as a tiebreaker."""
    duration_ok = [
        c for c in candidates
        if _duration_matches(track, c.get("duration"))
    ]
    if not duration_ok:
        return None

    artist_norm = _normalize(track.primary_artist)

    def is_topic_channel(c):
        channel_norm = _normalize(c.get("channel") or c.get("uploader") or "")
        return "topic" in channel_norm and artist_norm in channel_norm

    def score(c):
        title = c.get("title", "") or ""
        channel = c.get("channel") or c.get("uploader") or ""
        title_norm = _normalize(title)

        has_official_audio = any(
            phrase in title_norm for phrase in ("official audio", "hq", "high quality")
        )
        similarity = _text_similarity(track, title, channel)

        # Topic-channel match outranks title-keyword match, which
        # outranks plain similarity -- encoded as tiers within the score.
        return (is_topic_channel(c), has_official_audio, similarity)

    # v0.17.7: with Prefer explicit versions on, a title marked explicit wins
    # over the same song unmarked or marked clean (plain YouTube has no
    # explicit badge). "Same song": same title without version words, and a
    # Topic channel only stands in for a Topic channel, so a reupload never
    # replaces an official upload because it says "Explicit".
    best = _explicit_pick(
        track, duration_ok, score,
        lambda c: c.get("title", "") or "",
        lambda c, b: (is_topic_channel(c) == is_topic_channel(b)
                      and _core_title(c.get("title", "")) == _core_title(b.get("title", ""))),
    )
    video_id = best.get("id")
    if not video_id:
        return None

    return MatchResult(
        video_id=video_id,
        title=best.get("title", ""),
        tier="youtube",
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def find_audio_version(track) -> Tuple[Optional[MatchResult], Optional[str]]:
    """
    The audio-only (ATV) upload of a YT Music album song that YT Music
    lists as a video (v0.16.51, Checklist item 42a; Michael's choice), when
    ytmusic_reader couldn't find it in the album's own audio playlist.
    `track` is a youtube_playlist_reader.YouTubeTrack.

    Stricter than Tier 1, since the listed video is a fine fallback: one
    YouTube Music "songs" search, and only an ATV result with the same
    title (case and punctuation aside), one of the same artists and a
    length within DURATION_TOLERANCE_SECONDS is taken; one on the same
    album wins. Returns (match, None) or (None, None) when there is none,
    and (None, error) when the search failed (after its retry) -- the
    caller then doesn't fall back to the video, as a lower tier is never
    accepted when a better one's search failed (v0.16.27).
    """
    candidates, error = _search_ytmusic_category(track, filter="songs")
    if error:
        return None, error
    return _strict_atv(track, candidates), None


def _strict_atv(track, candidates: List[dict]) -> Optional[MatchResult]:
    """The official-audio (ATV) candidate with the same title (case and
    punctuation aside) and one of the same artists, and -- when the track
    has a length -- within DURATION_TOLERANCE_SECONDS of it; one on the
    same album wins, then (with Prefer explicit versions on, v0.17.7) an
    explicit one. Shared by find_audio_version() (v0.16.51) and
    find_strict_match() (v0.16.54)."""
    title = _normalize(track.name)
    artists = {_normalize(a) for a in (track.artists or [track.primary_artist]) if a}
    album = _normalize(track.album_name or "")

    def artist_fits(c):
        names = [_normalize(a.get("name", "")) for a in (c.get("artists") or [])]
        return any(n and (n in artists or any(n in a or a in n for a in artists)) for n in names)

    def fits(c):
        return (c.get("videoType") == "MUSIC_VIDEO_TYPE_ATV"
                and _normalize(c.get("title", "")) == title
                and artist_fits(c)
                and (not track.duration_seconds or c.get("duration_seconds") is None
                     or abs(track.duration_seconds - c["duration_seconds"]) <= DURATION_TOLERANCE_SECONDS))

    good = [c for c in candidates if fits(c)]
    if not good:
        return None
    explicit_first = prefer_explicit()  # v0.17.7: after the album, the explicit version wins

    def rank(c):
        same_album = bool(album) and _normalize((c.get("album") or {}).get("name", "")) == album
        return (same_album, _explicit_rank(track, c, c.get("title", ""))) if explicit_first else same_album

    best = max(good, key=rank)
    return MatchResult(
        video_id=best["videoId"], title=best.get("title", ""), tier="ytmusic_atv",
        url=f"https://www.youtube.com/watch?v={best['videoId']}",
    )


def find_strict_match(track) -> Tuple[Optional[MatchResult], Optional[str], Optional[str]]:
    """
    A song with no known length (v0.16.54, Checklist item 42d: an Amazon
    Music song neither Deezer nor Apple has; Michael's choice). Without a
    length the ±5 s check of every tier can't run, so only YouTube Music's
    official audio upload (ATV) with the exact same title and one of the
    same artists is accepted -- no plain-YouTube or music-video tier.
    Returns (match, reason, kind) like find_best_match(): NO_MATCH when the
    search worked and nothing fitted (a case for an exception), the search
    error's kind when it failed.
    """
    candidates, error = _search_ytmusic_category(track, filter="songs")
    if error:
        return _search_failed(track, "YouTube Music", error)
    match = _strict_atv(track, candidates)
    if match:
        return match, None, None
    reason = ("Amazon Music gives no song length and neither Deezer nor Apple Music has this song, so only an "
              "official audio upload with exactly the same title and artist is accepted -- none was found "
              f"({len(candidates)} YouTube Music result(s) checked)")
    logger.log_error(f"No strict match for '{track.primary_artist} - {track.name}': {reason}")
    return None, reason, track_failures.NO_MATCH


def _search_failed(track: Track, which: str, error: str) -> Tuple[None, str, str]:
    kind = track_failures.classify_search_error(error)
    reason = f"{which} search failed ({error}), so the best match couldn't be checked"
    logger.log_error(f"Search failed for '{track.primary_artist} - {track.name}': {reason}")
    return None, reason, kind


def find_best_match(track: Track) -> Tuple[Optional[MatchResult], Optional[str], Optional[str]]:
    """
    Runs the full tiered strategy in order, stopping at the first tier
    that finds a confident match.

    Returns (match, failure_reason, failure_kind). On success, match is
    set and the other two are None. On failure, match is None,
    failure_reason is a specific, human-readable summary of what was
    tried and why nothing was accepted -- e.g. "Found 8 YouTube Music
    candidates and 12 plain YouTube candidates, but none matched
    Spotify's duration (3:15) within 5 seconds" -- rather than a bare
    "no match," so the console/download log can show something
    genuinely useful instead of requiring a log-file dig, and
    failure_kind (track_failures) says which kind of failure it is:

      - NO_MATCH only when every search ran normally and nothing fitted
        -- a case for the Exceptions system;
      - SEARCH_FAILED / CONNECTION / BOT_CHECK when a search needed to
        decide still failed after its retry (a lower tier is never
        accepted in that case, see the module docstring);
      - TRACK_INFO when Spotify gave no duration to check against.
    """
    ytmusic_candidates, ytmusic_error = _search_ytmusic_candidates(track)
    atv_count = sum(1 for c in ytmusic_candidates if c.get("videoType") == "MUSIC_VIDEO_TYPE_ATV")
    omv_count = sum(1 for c in ytmusic_candidates if c.get("videoType") == "MUSIC_VIDEO_TYPE_OMV")

    match = try_ytmusic_atv(track, ytmusic_candidates)
    if match:
        return match, None, None
    if ytmusic_error:
        return _search_failed(track, "YouTube Music", ytmusic_error)

    plain_candidates, plain_error = _search_youtube_plain(track)
    match = try_youtube_plain(track, plain_candidates)
    if match:
        return match, None, None
    if plain_error:
        return _search_failed(track, "Plain YouTube", plain_error)

    match = try_ytmusic_omv(track, ytmusic_candidates)
    if match:
        return match, None, None

    if not track.duration_seconds:
        reason = (
            "No length is known for this track (Spotify gave none, or for Amazon Music the length lookup "
            "couldn't run this time), so none of the search results could be checked against it"
        )
        logger.log_error(f"No match checked for '{track.primary_artist} - {track.name}': {reason}")
        return None, reason, track_failures.TRACK_INFO

    duration_str = f"{int(track.duration_seconds // 60)}:{int(track.duration_seconds % 60):02d}"

    if not ytmusic_candidates and not plain_candidates:
        reason = (
            f"No YouTube Music or plain YouTube results at all for "
            f"\"{_search_query(track)}\" -- every search worked but came back empty."
        )
    elif atv_count == 0 and omv_count == 0 and not plain_candidates:
        reason = (
            f"Found {len(ytmusic_candidates)} YouTube Music result(s), but none were "
            f"official audio or video uploads (ATV/OMV) and plain YouTube search found nothing."
        )
    else:
        reason = (
            f"Found {atv_count} YouTube Music official-audio, {omv_count} official-video, "
            f"and {len(plain_candidates)} plain YouTube candidate(s), but none matched "
            f"Spotify's duration ({duration_str}) within {DURATION_TOLERANCE_SECONDS} seconds."
        )

    logger.log_error(f"No match found in any tier for '{track.primary_artist} - {track.name}': {reason}")
    return None, reason, track_failures.NO_MATCH
