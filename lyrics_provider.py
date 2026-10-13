"""
Mud Music Manager - Lyrics Provider

Optional plain-text lyrics lookup via the standalone `syncedlyrics`
package. This is NOT a spotDL dependency creeping back in -- it's an
independent, standalone library on PyPI that happened to also be one of
spotDL's dependencies. It works entirely on its own, searching multiple
lyrics sources itself.

Lyrics are "nice-to-have, not required" per the design doc (Section
3.1) -- a failed or missing lookup should never block the rest of the
acquisition/tagging pipeline, and this module never raises for that
reason.

v0.23.3: syncedlyrics is imported on the first lookup, not at app launch
(it took ~0.08 s of every launch; see spotify_reader's "Faster launch").
"""

from typing import Optional

import logger
from spotify_reader import Track


def get_lyrics(track: Track) -> Optional[str]:
    """
    Look up plain-text lyrics for a track. Returns None if nothing is
    found or the lookup fails for any reason -- callers should treat
    this as "no lyrics available this time," not an error worth
    surfacing to the user.
    """
    search_term = f"{track.primary_artist} - {track.name}"
    try:
        import syncedlyrics

        lyrics = syncedlyrics.search(search_term, plain_only=True)
    except Exception as e:
        logger.log_error(f"Lyrics lookup failed for '{search_term}': {e}")
        return None

    return lyrics
