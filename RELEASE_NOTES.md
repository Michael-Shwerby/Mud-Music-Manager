# Mud Music Manager — Release Notes

What changed in each version, newest first.

## v0.18.2 (2026-10-12)
- The data folder path in Settings can be selected and copied (right-click → Copy).

## v0.18.1 (2026-10-12)
- Settings → Your data now shows where your data folder is.

## v0.18.0 (2026-10-12)
- **Your data now has its own folder:** `%APPDATA%\Mud Music Manager` on Windows. This covers your playlists, exceptions, settings, pictures, logs and backups. This prepares the app for an installer and automatic updates, which can then replace the program without touching your data.
- The first time v0.18.0 starts, your existing data is copied there automatically. The old files are left where they were, with a note explaining the move; you can delete them once everything looks right. Your music files don't move.
- For a portable setup, put an empty `portable.txt` file next to the program to keep everything in the program's own folder.

## v0.17.9 (2026-10-12)
- **Export / Import in the terminal menu.** Settings → `export` / `import` work the same way as in the app. Import is unavailable in a terminal menu opened from the app's CMD button, because the running app would overwrite the imported settings. Close the app and run `python main.py --cli` instead.

## v0.17.8 (2026-10-12)
- **Export / Import your data** (Settings → Your data). It saves your playlists, albums, artists, exceptions, failure history, settings and custom pictures into one `.zip`, so a new install can start where you left off. Music files aren't included; a Sync downloads them again.
- Import replaces this computer's data. A safety copy of the current data is saved to `backups/` first, so an import can always be undone.

## v0.17.7 (2026-10-12)
- **New setting: Prefer explicit versions.** When a song has explicit and clean versions, the explicit one is chosen. It only switches between versions of the same song by the same artist. Off by default.

## v0.17.6 (2026-10-12)
- **Elapsed time and estimated time left** during a Sync, and the total time when it finishes.
- The progress bar gives each playlist space in proportion to its length.
- Each playlist's Sync Log starts with its source link and records how long each sync took.

## v0.17.5 (2026-10-11)
- **Mud Music Manager is open source** under the GNU GPL v3 or later. See `LICENSE.txt` and `THIRD_PARTY_NOTICES.txt`.

## v0.17.4 (2026-10-11)
- With the PNG cover art option, every cover is now a PNG. Covers that are only available as JPEG are converted losslessly with FFmpeg.
- **New setting: Smooth scrolling** (on by default).

## v0.17.3 (2026-10-11)
- **Cover art as PNG by default**, requested from sources that offer it (Apple Music, YT Music, Deezer). JPEG remains an option in Settings.

## v0.17.2 (2026-10-11)
- **Better default audio quality:** YouTube's AAC audio is now saved as-is instead of being converted from Opus, so there's no second lossy encode. "Best available" is still an option.

## v0.17.1 (2026-10-11)
- The app's own logo and icon (in the window, on the taskbar and in Windows shortcuts), and an **About** panel in Settings.

## v0.17.0 (2026-10-10)
- **Smoother animations:** playlist bars, the progress panel and lists slide open and closed; smooth scrolling; reordering slides into place; settings switches slide.
- Reordering playlists is now near-instant.
- Fixed a tiny window that briefly flashed when reordering lists that contain albums.

## v0.16.54 (2026-10-09)
- **Amazon Music playlists and albums.** Each song is matched on YouTube. Song lengths come from public music catalogues (Deezer, then Apple); songs without a known length are matched only to an exact official audio upload.

## v0.16.53 (2026-10-09)
- **Apple Music playlists, albums and artists**, read from Apple's public pages and catalogue. Each song is matched on YouTube.

## v0.16.52 (2026-10-09)
- **SoundCloud playlists and albums.** Songs download from SoundCloud when full-length downloads are offered. Otherwise (previews or protected uploads) the song is matched on YouTube.

## v0.16.51 (2026-10-09)
- **YT Music playlists, albums and artists.** On albums, music-video entries are swapped for the official audio-only version when one exists.

## v0.16.50 (2026-10-09)
- **Artist discographies:** paste an artist link, pick which albums and singles to add (all ticked by default), and they're added and synced one after another.
- **Watch an artist:** new releases are added and downloaded automatically. Releases you skip or remove are never re-added.

## v0.16.49 (2026-10-09)
- Albums now appear in their own **"Albums & discographies"** section below your playlists.

## v0.16.48 (2026-10-09)
- **Spotify album links** can be tracked like playlists. Folders are named "Artist - Album", and songs get the album's track and disc numbers.

## v0.16.47 (2026-10-09)
- **Terminal menu catches up with the app:** change download quality and YouTube sign-in from Settings; song names in the Exceptions list; Ctrl+C stops a sync safely; better cleanup after Remove and link changes.

## v0.16.45 (2026-10-09)
- **More file formats:** m4a (default), mp3, ogg, flac and wav. Songs already downloaded aren't converted. A folder can mix formats.

## v0.16.44 (2026-10-09)
- **Download quality settings:** source audio, conversion bitrate, speed limit and an optional pause between downloads, with Reset to defaults.

## v0.16.43 (2026-10-09)
- The Edit page's side list is split into collapsible **Exceptions** and **Issues / Pending** lists.

## v0.16.42 (2026-10-09)
- **Optional YouTube sign-in** for videos that require an account (for example, age-restricted videos), using your browser's cookies or a cookies.txt file. Downloads stay signed out unless a video needs it. Cookie values are never logged or shown.

## v0.16.41 (2026-10-09)
- The "unavailable on Spotify" note is shown on the Edit page only.

## v0.16.40 (2026-10-09)
- **Use your own audio file** for any song: it's copied (or converted to .m4a) into the playlist and tagged like a download. Your original file is never changed.
- Saving exceptions is much faster on large playlists.

## v0.16.39 (2026-10-09)
- Songs that a playlist lists but that aren't available in your region are now shown in the app, with the reason, instead of silently missing.

## v0.16.38 (2026-10-09)
- **Age-restricted** videos get their own label, and exception links are checked as you paste them.
- Fixed: a failed exception download no longer removes the song's existing file.

## v0.16.37 (2026-10-08)
- Fixed large playlists never settling: two songs with the same title no longer overwrite each other (the second is saved as "Title (2)"), and track numbers stay correct when a playlist contains an unavailable song.

## v0.16.36 (2026-10-08)
- **Stop just the current playlist** during Sync Now; the rest carry on.

## v0.16.35 (2026-10-08)
- A single click selects a playlist; double-click, right-click or clicking it again expands it.

## v0.16.34 (2026-10-08)
- Slimmer playlist bars on the Main Menu.

## v0.16.33 (2026-10-08)
- **Queue more playlists** while one is still being added; they're added and synced in order.

## v0.16.32 (2026-10-08)
- Search improvements: clicking a result selects it, and clearing a search scrolls back to your selection.

## v0.16.31 (2026-10-08)
- **Search songs** on the Edit page (Ctrl+F or /).

## v0.16.30 (2026-10-08)
- **Search playlists** on the Main Menu (Ctrl+F or /).

## v0.16.29 (2026-10-08)
- **Expand all / Collapse all** buttons (O / Shift+O).

## v0.16.28 (2026-10-08)
- Clicking a playlist moves the keyboard selection to it.

## v0.16.27 (2026-10-07)
- **More reliable downloads:** temporary download errors are retried automatically, and a failed search is never mistaken for "no match".
- **Clear failure labels:** No match, Download blocked, Video unavailable, No connection and others. A song is marked **Unavailable** after 5 failed syncs (it's still tried each time).

## v0.16.26 (2026-10-07)
- **Remap keyboard controls** in Settings, with a safety countdown for essential keys.

## v0.16.25 (2026-10-07)
- Press **Tab** to see the keyboard shortcuts for the current screen.

## v0.16.24 (2026-10-07)
- Keyboard control in Settings, results and dialogs. Dialogs start on their safe button (e.g. Cancel).

## v0.16.23 (2026-10-07)
- Keyboard control on the Edit page (songs, player, exceptions, Save).

## v0.16.22 (2026-10-07)
- Shift+W / Shift+S move the selected playlist up or down.

## v0.16.21 (2026-10-07)
- Enter on a playlist lets you type its new position.

## v0.16.20 (2026-10-07)
- **Keyboard control** on the Main Menu (W/A/S/D or arrow keys, Space and single-key shortcuts).

## v0.16.19 (2026-10-07)
- **Much faster playlist reads:** a Scan of a 20-song Spotify playlist takes about 2 s instead of ~15 s.

## v0.16.18 (2026-10-07)
- The app appears in the Windows volume mixer from launch.

## v0.16.17 (2026-10-07)
- **New setting: Scan on launch** (on by default).

## v0.16.16 (2026-10-07)
- Playlists start unticked when choosing what to Sync, Scan or Check.

## v0.16.15 (2026-10-07)
- Fixed a typing cursor appearing in a playlist's position box after clicking certain buttons.

## v0.16.14 (2026-10-07)
- **Cancel Scan, Use Cache:** stop a scan and keep the numbers already shown.

## v0.16.13 (2026-10-07)
- Settings and results open as an overlay over the current screen.

## v0.16.12 (2026-10-07)
- Change a playlist's link on the Edit page, including switching between Spotify and YouTube.

## v0.16.11 (2026-10-07)
- A large "Sync Complete" banner when a sync finishes.

## v0.16.10 (2026-10-07)
- Larger Add / Remove buttons and status pills.

## v0.16.9 (2026-10-07)
- Centred player controls and a volume slider on the Edit page.

## v0.16.8 (2026-10-07)
- Fixed playlist bars being cut off in narrow windows.

## v0.16.7 (2026-10-06)
- Fixed the CMD button's in-app window. It now closes when the menu exits.

## v0.16.6 (2026-10-06)
- Fixed an intermittent crash during long syncs.

## v0.16.5 (2026-10-05)
- New launcher, `Mud Music Manager.pyw`, for a faster start.

## v0.16.4 (2026-10-05)
- Fixed the Debug Window opening blank.

## v0.16.3 (2026-10-05)
- No extra console window on launch, and Settings → **Open Debug Window** for live logs.

## v0.16.2 (2026-10-05)
- Enter now confirms the Remove dialog. Added crash logging (`logs/crash_log.txt`).

## v0.16.1 (2026-10-05)
- Fixed Add, Remove and Change Folder doing nothing after their dialogs were confirmed.

## v0.16.0
- **The graphical app.** `python main.py` opens a window; the terminal menu is still available with `--cli` or the CMD button.
- **Requirements changed:** run `pip install -r requirements.txt` (adds PySide6).

## v0.15.0
- **YouTube playlists** can be synced alongside Spotify playlists; the platform is detected from the link.

## v0.14.0
- A platform tag beside each playlist in the terminal menu.
- **Requirements changed:** adds colorama.

## v0.13.0
- The YouTube video ID of each download is visible in Windows Explorer (Composer field).

## v0.12.0
- Changing the Music folder can move your playlist folders there safely, or just point the app at the new location.

## v0.11.0
- **Crash safety:** saves, downloads, renames and retagging can't leave half-written files if the app is closed mid-way.
- New **Check Integrity** tool.

## v0.10.0
- New **Scan**: see what a Sync would do without changing anything.

## v0.9.1
- A ✅ / ✗ status on the library update line.

## v0.9.0
- Keyword-based menus, a folder picker and playlist reordering.

## v0.8.0
- Redesigned menus; connect folders you already downloaded; specific failure reasons; a per-playlist Sync Log; library update checks.

## v0.7.0
- Sync, track fixing and exceptions from the menu, with live progress.

## v0.6.0 – v0.6.1
- **Exceptions:** choose the exact YouTube video for a song. Optional limit on new downloads per sync.

## v0.5.0
- **Library sync:** new songs downloaded, removed songs taken out, track numbers kept in playlist order.

## v0.4.0 – v0.4.1
- Full tagging (title, artist, album, year, track number, cover art) and lyrics. Files are named by song title.

## v0.3.0 – v0.3.1
- The app's own matching engine: official audio first, then other uploads, always within 5 seconds of the song's length.

## v0.1.0 – v0.2.3
- First versions: playlist tracking, logging, and early download experiments.
