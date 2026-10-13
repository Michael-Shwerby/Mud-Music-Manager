# Mud Music Manager

A desktop app (Python, PySide6) that keeps a local music folder in step with
a playlist or album on Spotify, YouTube, YT Music, SoundCloud, Apple Music or
Amazon Music: new songs are added, removed songs are taken out, track numbers
follow the playlist's order, and every file is tagged (title, artist, album,
year, cover art, lyrics).

## Requirements

- Python 3 (developed and tested on Python 3.14, on Windows)
- The libraries in `requirements.txt`: `pip install -r requirements.txt`
- [FFmpeg](https://ffmpeg.org) on your PATH

## Running

- Double-click `Mud Music Manager.pyw`, or run `python main.py` (graphical app)
- `python main.py --cli` opens the terminal menu

## Your responsibility

This app downloads audio from online services. You are responsible for using
it in line with those services' terms and the copyright law that applies to
you. Only download material you have the right to copy.

Mud Music Manager is not affiliated with, sponsored or endorsed by Spotify,
YouTube, SoundCloud, Apple or Amazon.

## Support the project

Mud Music Manager is free. If it's useful to you, you can support its
development with a donation: use the **Sponsor** button at the top of this
repository's GitHub page.

## License

Copyright (c) 2026 Michael Xuereb.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see [LICENSE.txt](LICENSE.txt).

"Mud Music Manager" and the MMM logo and emblem are trademarks of Michael
Xuereb and are not covered by the licence. Third-party libraries keep their
own licences; see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
