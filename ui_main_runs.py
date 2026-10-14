"""
Mud Music Manager - Main Menu runs (split out of ui_main_page.py in v0.23.4)

MainPageRunsMixin: the MainPage methods that start and follow Sync Now,
Add runs and the Add queue, Scan and Check Integrity. A mixin, so every
method is still a MainPage method (self is the page) and nothing about
how they run changed in the split.
"""

import os
from dataclasses import replace
from typing import List, Optional

import config_manager
import device_copy
import scheduled_sync
import library_sync
import track_failures
import ui_backend
import ui_dialogs
import ui_settings
from ui_main_rows import _park_focus
from ui_sync_panel import bar_weights, QueuedAdd, QueuedSync


class MainPageRunsMixin:
    # ------------------------------------------------------------------
    # Running things
    # ------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        for b in (self.sync_btn, self.scan_btn, self.integrity_btn, self.copy_btn, self.add_btn, self.remove_btn,
                  self.change_btn):
            if busy:
                _park_focus(b, self)
            b.setEnabled(not busy)
        if busy:  # the launch-time scan is being stopped for this task anyway
            _park_focus(self.use_cache_btn, self)
            self.use_cache_btn.hide()
        for row in list(self._rows.values()) + list(self._artist_rows.values()):
            row.set_controls_enabled(not busy)
        self._update_add_btn()

    def _update_add_btn(self) -> None:
        """While playlists are being added (v0.16.33, Checklist item 34) the
        Add button stays usable as "Queue Another Playlist"; after Cancel
        all, it waits for the run to stop."""
        queueing = self._adds is not None
        self.add_btn.setText("+  Queue Another Playlist" if queueing else "+  Add")
        self.add_btn.setToolTip("Add another playlist once the ones ahead of it are done" if queueing else "")
        if queueing:
            usable = not self._adds_cancel_all
            if not usable:
                _park_focus(self.add_btn, self)
            self.add_btn.setEnabled(usable)

    def _need_library(self, entries: List[dict]) -> bool:
        """Entries without a custom_path live under the Music folder, so
        one has to be set (the terminal menu says the same)."""
        if config_manager.get_library_path(self.config):
            return True
        if all(e.get("custom_path") for e in entries):
            return True
        if ui_dialogs.ask_yes_no(
            self, "No Music folder yet",
            "No Music folder is set yet. Choose one first?", "Choose folder", "Cancel",
        ):
            self._on_change_folder()
        return False

    # ---- Scheduled sync in the open app (v0.25.0, Checklist item 65) ----
    def start_scheduled_sync(self) -> Optional[str]:
        """Sync Now of every playlist, started by the scheduled task while
        the app is open (MainWindow.run_scheduled_sync(), which only calls
        this when nothing else is running). None when it started, else why
        it couldn't (recorded and notified by the caller)."""
        entries = list(config_manager.list_playlists(self.config))
        if not entries:
            return "No playlists to sync."
        if not config_manager.get_library_path(self.config) and not all(e.get("custom_path") for e in entries):
            return "Skipped: no Music folder is set."
        if self._mode:
            self._exit_mode()
        if self._remove_mode:
            self._toggle_remove_mode()
        self._scheduled_run = True
        self._start_sync(entries)
        if not self.backend.heavy_running and self._control is None and not self._syncs:
            self._scheduled_run = False
            return "Skipped: the app was busy."
        return None

    def _scheduled_done(self, results: list, cancelled: bool = False, failed: str = "") -> None:
        """A scheduled Sync Now ended: record it and say so."""
        if not getattr(self, "_scheduled_run", False):
            return
        self._scheduled_run = False
        if failed:
            summary, ok = f"Couldn't sync: {failed}", False
        else:
            done = [r.result for r in results if r.result is not None]
            errors = sum(1 for r in results if r.error)
            stopped = cancelled or any(r.stopped for r in results)
            summary, ok = scheduled_sync.summarize(done, errors, stopped)
        scheduled_sync.record_result(summary, ok, in_app=True)
        window = getattr(self.ctx, "window", None)
        tray = window.tray_icon() if window is not None and hasattr(window, "tray_icon") else None
        if tray is not None:
            scheduled_sync.notify("Scheduled sync finished" if ok else "Scheduled sync: needs a look", summary, tray)

    def _start_sync(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "sync"
        self.panel.show_running("Sync Progress:", len(entries), True, True, "Preparing…")
        self._syncs = [QueuedSync(e) for e in entries]
        self.panel.bar.set_weights(bar_weights(self._syncs))  # v0.17.6: sized by track count
        self.panel.start_clock()
        self.set_busy(True)
        self._refresh_sync_ui()
        self.backend.when_refresh_stopped(lambda: self._launch_sync(entries))

    def _launch_sync(self, entries: List[dict]) -> None:
        self._control = ui_backend.SyncControl(self)
        self._control.stateChanged.connect(self.panel.set_control_state)
        thread = self.backend.run(ui_backend.sync_task(self.config, entries, self._control,
                                                       self.apply_new_releases_call))  # v0.16.50
        thread.progressInfo.connect(self._on_sync_progress)
        thread.entryStarted.connect(self._on_sync_entry_started)
        thread.entryDone.connect(self._on_sync_entry_done)
        thread.localStats.connect(self.on_local_stats)
        thread.resultReady.connect(self._on_sync_finished)
        thread.cancelled.connect(self._on_sync_cancelled)
        thread.failed.connect(self._on_task_failed)

    def _on_sync_entry_started(self, entry: dict) -> None:
        self.on_entry_started(entry, syncing=True)
        item = self._sync_item(entry)
        if item is not None:
            item.state, item.text = "syncing", "Preparing…"
            self._refresh_sync_ui()

    # ---- Sync Now's queue list (v0.16.36, Checklist item 37) ----------
    def _sync_item(self, entry: dict) -> Optional[QueuedSync]:
        key = ui_settings.playlist_key(entry)
        return next((i for i in self._syncs or [] if i.key == key), None)

    def _refresh_sync_ui(self) -> None:
        """Sync Now's playlists changed state: rebuild the queue list, and
        Cancel reads Cancel all while some are waiting."""
        if self._syncs is None:
            return
        self.panel.set_queue(self._syncs, live=True)
        # more than one left (the first may not have started yet)
        several = sum(1 for i in self._syncs if i.state in ("waiting", "syncing")) > 1
        if self._control is None or not self._control.cancel_requested:
            self.panel.cancel_btn.setText("Cancel all" if several else "Cancel")
        self.panel.cancel_btn.setToolTip(
            "Stop the playlist being synced and skip the ones waiting" if several else "")

    def _on_sync_progress(self, p) -> None:
        """The usual progress line, and the same news on the queue line of
        the playlist being synced. v0.17.6: once it has been read, its
        segment of the bar takes its real track count."""
        item = next((i for i in self._syncs or [] if i.state == "syncing"), None)
        if item is not None and p.track_total and item.size != p.track_total:
            item.size = p.track_total
            self.panel.bar.set_weights(bar_weights(self._syncs))
        self.panel.update_progress(p)
        if item is None or item.stopping or (self._control is not None and self._control.cancel_requested):
            return
        parts = [p.text] if p.text else []
        if p.track_total:
            parts.append(f"Track {p.track_index}/{p.track_total}")
        item.text = "  ·  ".join(parts)
        self.panel.update_queue_item(item)

    def _on_sync_entry_done(self, run) -> None:
        self.on_entry_done(run)
        item = self._sync_item(run.entry)
        if item is None:
            return
        item.run, item.stopping = run, False
        if run.stopped:
            item.state = "stopped"
            item.text = (f"Stopped · {run.stopped_downloaded} downloaded before stopping. "
                         "Sync Now finishes it.")
        elif run.error:
            item.state, item.text = "sync_failed", f"Sync failed: {run.error}"
        else:
            item.state, item.text = "synced", self._run_result_line(run)
        self._refresh_sync_ui()

    def _on_sync_item_stop(self, item: QueuedSync) -> None:
        """✕ on the playlist being synced: stop that playlist only, at the
        next safe point (after the current download); the rest carry on."""
        if self._syncs is None or item not in self._syncs or item.state != "syncing" or self._control is None:
            return
        _park_focus(self.panel, self)
        if self._control.request_stop_playlist(item.key):
            item.stopping = True
            item.text = "Stopping this playlist — finishing the current download…"
            self._refresh_sync_ui()

    def _end_sync_queue(self, cancelled: bool = False, failed: str = "") -> None:
        """Sync Now ended: the list stays readable (no ✕) until Dismiss."""
        items, self._syncs = self._syncs, None
        if not items:
            return
        for item in items:
            if item.state == "syncing":
                if failed:
                    item.state, item.text = "sync_failed", f"Something went wrong: {failed}"
                else:
                    item.state, item.text = "stopped", "Stopped by Cancel. Sync Now finishes it."
            elif item.state == "waiting":
                item.state, item.text = "not_synced", "Not synced (Cancel)" if cancelled else "Not synced"
        self.panel.set_queue(items, live=False)

    def _on_pause_clicked(self) -> None:
        if not self._control:
            return
        _park_focus(self.panel, self)  # the button is disabled while pausing
        if self._control.state == "paused":
            self._control.request_resume()
        else:
            self._control.request_pause()

    def _on_cancel_clicked(self) -> None:
        if self._scan_control is not None:
            self._cancel_scan_use_cache()
        elif self._adds is not None:
            self._cancel_all_adds()
        elif self._control:
            _park_focus(self.panel, self)  # the button is disabled while cancelling
            self._control.request_cancel()
            for item in self._syncs or []:  # v0.16.36: no ✕ while it all stops
                if item.state == "syncing":
                    item.stopping = True
                    item.text = "Stopping — finishing the current download…"
            self._refresh_sync_ui()

    def _sync_report(self, results: list, cancelled: bool) -> tuple:
        """Totals and the Sync Details text. The failed tracks are grouped
        by what they need (v0.16.27, Checklist item 28): No match (an
        exception link), Unavailable (failed on 5+ syncs), or failed this
        time (usually temporary). attention is the short summary for the
        panel, e.g. " · 1 no match · 2 will retry"."""
        downloaded = renumbered = removed = unchanged = 0
        lines = []
        failed_total = 0
        errors = 0
        counts = {track_failures.STATUS_NO_MATCH: 0, track_failures.STATUS_UNAVAILABLE: 0,
                  track_failures.STATUS_RETRY: 0, "other": 0}
        stopped = 0
        for run in results:
            if run.stopped:  # v0.16.36: ✕ stopped this playlist's sync
                stopped += 1
                downloaded += run.stopped_downloaded
                lines.append(f"=== {run.name} ===\nStopped before it finished ({run.stopped_downloaded} "
                             "downloaded first; nothing removed or renumbered). Sync Now finishes it.\n")
                continue
            if run.error:
                errors += 1
                lines.append(f"=== {run.name} ===\nSync failed: {run.error}\n")
                continue
            r = run.result
            ok_dl = sum(1 for d in r.downloaded if d.file_path is not None)
            downloaded += ok_dl
            renumbered += len(r.renumbered)
            removed += len(r.removed)
            unchanged += r.unchanged_count
            failed_total += len(r.failed)
            lines.append(
                f"=== {run.name} ===\n{ok_dl} downloaded, {len(r.renumbered)} renumbered, "
                f"{len(r.removed)} removed, {r.unchanged_count} unchanged, {len(r.failed)} failed/unmatched"
            )
            groups = {track_failures.STATUS_NO_MATCH: [], track_failures.STATUS_UNAVAILABLE: [],
                      track_failures.STATUS_RETRY: []}
            for d in r.failure_details:
                groups[track_failures.status(d)].append(f"  - {d.get('name', '')}: {track_failures.summary(d)}")
            other = [f"  - {n}: {reason}" for n, reason in r.failed if reason == library_sync.TRACK_NUMBER_FAILURE]
            for key, items in groups.items():
                counts[key] += len(items)
            counts["other"] += len(other)
            if groups[track_failures.STATUS_NO_MATCH]:
                lines.append("No match: the search worked, but nothing found was the right length. "
                             "Add an exception link on this playlist's Edit page:")
                lines.extend(groups[track_failures.STATUS_NO_MATCH])
            if groups[track_failures.STATUS_UNAVAILABLE]:
                lines.append(f"Unavailable: age-restricted, or failed on {track_failures.UNAVAILABLE_AFTER} or "
                             "more syncs (still tried on every sync; an exception link may help):")
                lines.extend(groups[track_failures.STATUS_UNAVAILABLE])
            if groups[track_failures.STATUS_RETRY]:
                lines.append("Failed this time, usually temporary (the next sync tries again):")
                lines.extend(groups[track_failures.STATUS_RETRY])
            if other:
                lines.append("Other problems:")
                lines.extend(other)
            lines.append("")
        nothing = (not results) or (
            errors == 0 and failed_total == 0 and stopped == 0 and downloaded == renumbered == removed == 0
        )
        attention = ""
        for key, word in ((track_failures.STATUS_NO_MATCH, "no match"),
                          (track_failures.STATUS_UNAVAILABLE, "unavailable"),
                          (track_failures.STATUS_RETRY, "will retry"),
                          ("other", "other problem" + ("s" if counts["other"] != 1 else ""))):
            if counts[key]:
                attention += f" · {counts[key]} {word}"
        return downloaded, renumbered, removed, failed_total, errors, nothing, "\n".join(lines), attention

    def _on_sync_finished(self, results: list) -> None:
        downloaded, renumbered, removed, failed, errors, nothing, details, attention = self._sync_report(results, False)
        stopped = sum(1 for r in results if r.stopped)
        self._control = None
        self.set_busy(False)
        self.panel.set_bar(self.panel.bar._segments)
        self._last_sync_report = details
        if nothing:
            self.panel.show_finished("Everything is already in sync — nothing to download.", "",
                                     auto_hide_ms=9000, banner="Sync Complete")
        elif stopped and stopped == len(results):
            # v0.16.36: every playlist was stopped with its ✕
            self.panel.show_finished(f"Sync stopped — {downloaded} downloaded before stopping.", details)
        else:
            # "Sync Complete" is shown large in the bar, so the line above
            # carries only the numbers.
            text = f"{downloaded} downloaded, {renumbered} renumbered, {removed} removed"
            if failed:
                text += attention
            if errors:
                text += f" · {errors} playlist(s) couldn't be synced"
            if stopped:
                text += f" · {stopped} stopped"
            self.panel.show_finished(text, details, auto_hide_ms=0 if (failed or errors or stopped) else 15000,
                                     banner="Sync Complete")
        self._end_sync_queue()
        self.refresh_status_card()
        self.reload_stats_only()
        self._scheduled_done(results)  # v0.25.0
        if stopped:
            self.refreshRequested.emit()  # a stopped sync leaves the cached numbers stale

    def _on_sync_cancelled(self, partial: list) -> None:
        downloaded, renumbered, removed, failed, errors, nothing, details, _attention = self._sync_report(partial, True)
        self._control = None
        self.set_busy(False)
        self._last_sync_report = details
        text = f"Sync cancelled — {downloaded} downloaded before stopping."
        self.panel.show_finished(text, details)
        self._end_sync_queue(cancelled=True)
        for key, st in list(self._status.items()):
            if st.get("state") in ("syncing", "checking"):
                self._status[key] = {"state": "unknown"}
        for row in self._rows.values():
            self._apply_cached_status(row)
        self.refresh_status_card()
        self.reload_stats_only()
        self._scheduled_done(partial, cancelled=True)  # v0.25.0
        self.refreshRequested.emit()  # a cancelled sync leaves the cached numbers stale

    def _on_task_failed(self, message: str) -> None:
        self._control = None
        self._scan_control = None
        self._discard_scan_updates = False
        self.set_busy(False)
        self.panel.show_finished(f"Something went wrong: {message}", message)
        self._end_sync_queue(failed=message)
        for key, st in list(self._status.items()):
            if st.get("state") in ("syncing", "checking"):
                self._status[key] = {"state": "unknown"}
        for row in self._rows.values():
            self._apply_cached_status(row)
        self.refresh_status_card()
        self._scheduled_done([], failed=message)  # v0.25.0 (no-op unless a scheduled sync was running)

    # ------------------------------------------------------------------
    # Add runs and the Add queue (v0.16.33, Checklist item 34)
    # ------------------------------------------------------------------
    # An Add run syncs the playlist just added, then each queued one in
    # turn: config_manager.add_playlist() when its turn comes, then its own
    # sync_task (the same sync as Sync Now), with its own pause / cancel.
    # A queued playlist that can't be added, or whose sync fails, is marked
    # so in the queue list and the run carries on with the next one.

    def _queue_problem(self, data: dict) -> Optional[str]:
        """Why this playlist can't be queued (the checks add_playlist()
        makes, plus the playlists already waiting), or None. Checked now so
        a typo is caught while the dialog is still at hand."""
        source = config_manager.detect_source(data["source"])  # v0.16.48: albums too
        if source is None:
            return config_manager.unrecognised_source_message(data["source"])
        name = data["name"].strip()
        if not name:
            return "Folder name cannot be empty."
        existing = config_manager.find_playlist(self.config, name)
        if existing:
            return f"A tracked playlist already uses the name '{existing['folder_name']}'."
        for entry in config_manager.list_playlists(self.config):
            if config_manager.same_source(config_manager.entry_source(entry), source):  # v0.16.52
                return f"That {'album' if config_manager.is_album(entry) else 'playlist'} " \
                       f"is already tracked as '{entry['folder_name']}'."
        for item in self._adds or []:
            if item.state != "waiting":
                continue  # the ones already added are covered above
            if item.name.strip().lower() == name.lower():
                return f"A queued playlist already uses the name '{item.name}'."
            if config_manager.same_source(config_manager.detect_source(item.data["source"]), source):
                return f"That playlist is already queued as '{item.name}'."
        if data.get("custom_path"):
            if not os.path.isdir(data["custom_path"]):
                return f"That folder doesn't exist: {data['custom_path']}"
        elif not config_manager.get_library_path(self.config):
            return ("No Music folder is set yet, so there's nowhere to download to. Once this add "
                    "finishes, choose one with Change, then add this playlist.")
        return None

    def _refresh_queue_ui(self) -> None:
        """The queue changed (a playlist queued, taken out, started or
        done): rebuild the list and size the bar to the playlists counted."""
        if self._adds is None:
            return
        self.panel.set_queue(self._adds, live=True)
        counted = self._counted_adds()
        self.panel.bar.set_weights(bar_weights(counted))  # v0.17.6: sized by track count
        waiting = any(i.state == "waiting" for i in self._adds)
        if self._control is None or not self._control.cancel_requested:
            self.panel.cancel_btn.setText("Cancel all" if waiting else "Cancel")
        self.panel.cancel_btn.setToolTip(
            "Stop the playlist being added and drop the ones waiting" if waiting else "")

    def _counted_adds(self) -> List[QueuedAdd]:
        """The playlists the bar has a segment for (not those dropped by Cancel all)."""
        return [i for i in self._adds or [] if i.state != "cancelled"]

    def _start_add_run(self, entry: dict, data: dict) -> None:
        """The playlist was just added to playlists.json: sync it, with
        "Queue Another Playlist" usable until the run ends."""
        if self.backend.heavy_running or not self._need_library([entry]):
            return
        item = QueuedAdd(data)
        item.entry, item.state, item.text = entry, "adding", "Preparing…"
        self._adds, self._add_current, self._adds_cancel_all = [item], item, False
        self._running_kind = "sync"
        self.panel.show_running("Sync Progress:", 1, True, True, "Preparing…")
        self.panel.start_clock()  # v0.17.6: one clock for the whole Add run
        self.set_busy(True)
        self._refresh_queue_ui()
        self.backend.when_refresh_stopped(lambda: self._launch_add_item(item))

    def _launch_add_item(self, item: QueuedAdd) -> None:
        if self._adds is None or item is not self._add_current:
            return
        if self._adds_cancel_all:  # Cancel all before its sync could start
            item.state, item.text = "stopped", "Added, not synced yet (cancelled). Sync Now syncs it."
            self._finish_add_run()
            return
        # A pause asked for during the previous playlist holds the queue:
        # this one pauses before it starts reading (asked before its thread
        # starts, so it can't get past that first safe point).
        pause = self._control is not None and self._control.pause_requested
        self.panel.reset_controls()
        self._control = ui_backend.SyncControl(self)
        self._control.stateChanged.connect(self.panel.set_control_state)
        if pause:
            self._control.request_pause()
        thread = self.backend.run(ui_backend.sync_task(self.config, [item.entry], self._control))
        thread.progressInfo.connect(self._on_add_progress)
        thread.entryStarted.connect(self._on_sync_entry_started)
        thread.entryDone.connect(self.on_entry_done)
        thread.localStats.connect(self.on_local_stats)
        thread.resultReady.connect(self._on_add_item_finished)
        thread.cancelled.connect(self._on_add_item_cancelled)
        thread.failed.connect(self._on_add_item_task_failed)
        self._refresh_queue_ui()

    def _on_add_progress(self, p) -> None:
        """The usual progress line, counted over the whole Add run
        ("Playlist 2/3"), and the same news on the playlist's queue line.
        v0.17.6: the segments are sized by track count (this playlist's, once
        read); the ETA covers this playlist, with "+" while others wait."""
        item = self._add_current
        if self._adds is None or item is None:
            return
        counted = self._counted_adds()
        index = counted.index(item) if item in counted else 0
        if p.track_total:
            item.size = p.track_total
        self.panel.bar.set_weights(bar_weights(counted))
        waiting = any(i.state == "waiting" for i in counted)
        self.panel.update_progress(replace(p, playlist_index=index + 1, playlist_total=len(counted),
                                           eta_open=p.eta_open or waiting))
        parts = [p.text] if p.text else []
        if p.track_total:
            parts.append(f"Track {p.track_index}/{p.track_total}")
        if not (self._control is not None and self._control.cancel_requested):
            item.text = "  ·  ".join(parts)
            self.panel.update_queue_item(item)

    def _run_result_line(self, run) -> str:
        """One playlist's result for its queue line (Add run or Sync Now)."""
        downloaded, renumbered, removed, _failed, _errors, nothing, _details, attention = self._sync_report([run], False)
        if nothing:
            return "Nothing to download"
        return f"{downloaded} downloaded{attention}"

    def _on_add_item_finished(self, results: list) -> None:
        item = self._add_current
        if item is None:
            return
        run = results[0] if results else None
        item.run = run
        if run is not None and run.error:
            item.state, item.text = "sync_failed", f"Added, but its sync failed: {run.error}"
        else:
            item.state = "added"
            item.text = self._run_result_line(run) if run is not None else ""
        self._next_add_item()

    def _forget_syncing_status(self, item: QueuedAdd) -> None:
        """A stopped or broken sync: the row shows its cached status again."""
        key = ui_settings.playlist_key(item.entry) if item.entry else None
        if key and self._status.get(key, {}).get("state") in ("syncing", "checking"):
            self._status[key] = {"state": "unknown"}
            row = self._rows.get(key)
            if row is not None:
                self._apply_cached_status(row)
            self.refresh_status_card()

    def _on_add_item_cancelled(self, partial: list) -> None:
        item = self._add_current
        if item is None:
            return
        item.run = partial[0] if partial else None
        item.state, item.text = "stopped", "Added; its sync was stopped. Sync Now finishes it."
        self._forget_syncing_status(item)
        self._next_add_item()

    def _on_add_item_task_failed(self, message: str) -> None:
        item = self._add_current
        if item is None:
            return
        item.state, item.text = "sync_failed", f"Added, but something went wrong: {message}"
        self._forget_syncing_status(item)
        self._next_add_item()

    def _next_add_item(self) -> None:
        """Add and sync the next playlist waiting, or end the run. One that
        add_playlist() refuses (e.g. its folder was deleted meanwhile) is
        marked "Not added" with the reason, and the next one goes."""
        self._add_current = None
        while not self._adds_cancel_all:
            item = next((i for i in self._adds if i.state == "waiting"), None)
            if item is None:
                break
            data = item.data
            try:
                entry = config_manager.add_playlist(
                    self.config, data["source"], data["name"], custom_path=data["custom_path"],
                    artist_id=data.get("artist_id"),  # v0.16.50: an artist's release
                    kind=data.get("kind"),  # v0.16.52: a SoundCloud album
                )
            except ValueError as e:
                item.state, item.text = "not_added", f"Not added: {e}"
                continue
            item.entry, item.state, item.text = entry, "adding", "Preparing…"
            self._add_current = item
            if not self._name_matches(entry["folder_name"]):
                self.search.clear()  # so the new playlist isn't hidden by the search
            self.reload()
            self._launch_add_item(item)
            return
        self._finish_add_run()

    def _on_queue_item_cancel(self, item: QueuedAdd) -> None:
        """✕ on a queue line: a waiting playlist leaves the queue (it was
        never added); the one being added stops at the next safe point
        (it stays added, as after Cancel) and the queue carries on.
        v0.16.36: on Sync Now's list, ✕ stops the playlist being synced."""
        if isinstance(item, QueuedSync):
            self._on_sync_item_stop(item)
            return
        if self._adds is None or item not in self._adds:
            return
        _park_focus(self.panel, self)
        if item.state == "waiting":
            self._adds.remove(item)
            self._refresh_queue_ui()
        elif item.state == "adding" and item is self._add_current:
            if self._control is None or self._control.cancel_requested:
                return
            item.text = "Stopping — finishing the current download…"
            self._control.request_cancel()
            self._refresh_queue_ui()

    def _cancel_all_adds(self) -> None:
        """The panel's Cancel (Cancel all while some are waiting): stop the
        playlist being added and drop the ones waiting."""
        if self._adds is None or self._adds_cancel_all:
            return
        self._adds_cancel_all = True
        for item in self._adds:
            if item.state == "waiting":
                item.state, item.text = "cancelled", "Not added (Cancel all)"
        _park_focus(self.panel, self)
        self._update_add_btn()
        self._refresh_queue_ui()
        if self._control is not None and not self._control.cancel_requested:
            self._control.request_cancel()

    def _finish_add_run(self) -> None:
        """Every playlist of the run is done: the usual Sync Complete (or
        cancelled) summary, and the queue list stays readable until the
        panel is dismissed."""
        items, cancel_all = self._adds or [], self._adds_cancel_all
        self._adds, self._add_current, self._adds_cancel_all = None, None, False
        self._control = None
        self.set_busy(False)
        self.panel.set_bar(self.panel.bar._segments)
        runs = [i.run for i in items if i.run is not None]
        downloaded, renumbered, removed, failed, errors, nothing, details, attention = self._sync_report(runs, cancel_all)
        not_added = [i for i in items if i.state == "not_added"]
        stopped = [i for i in items if i.state == "stopped"]
        added = sum(1 for i in items if i.entry is not None)
        many = len(items) > 1
        if not_added:
            details = "\n".join([details] + [f"=== {i.name} ===\n{i.text}\n" for i in not_added]).strip("\n")
        self._last_sync_report = details
        if cancel_all or (stopped and len(stopped) == added and not not_added):
            text = f"Sync cancelled — {downloaded} downloaded before stopping."
            if many:
                text += f" {added} of {len(items)} playlists added."
            self.panel.show_finished(text, details)
        elif nothing and not (not_added or stopped):
            self.panel.show_finished("Everything is already in sync — nothing to download." if not many else
                                     f"{added} playlists added — nothing to download.", "",
                                     auto_hide_ms=9000, banner="Sync Complete")
        else:
            text = f"{downloaded} downloaded, {renumbered} renumbered, {removed} removed"
            if many:
                text = f"{added} playlist{'s' if added != 1 else ''} added · " + text
            if failed:
                text += attention
            if errors:
                text += f" · {errors} playlist(s) couldn't be synced"
            if not_added:
                text += f" · {len(not_added)} couldn't be added"
            if stopped:
                text += f" · {len(stopped)} stopped"
            quiet = not (failed or errors or not_added or stopped)
            self.panel.show_finished(text, details, auto_hide_ms=15000 if quiet else 0,
                                     banner="Sync Complete")
        self.panel.set_queue(items, live=False)
        self.refresh_status_card()
        self.reload_stats_only()
        if stopped:
            self.refreshRequested.emit()  # a stopped sync leaves the cached numbers stale

    def reload_stats_only(self) -> None:
        for row in self._rows.values():
            row.refresh_text(ui_settings.get_cache(row.key) or None)

    def _show_details(self) -> None:
        ui_dialogs.TextViewerDialog(self, "Sync details", self._last_sync_report or self.panel._details_text).exec()

    def _start_scan(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "scan"
        control = ui_backend.SyncControl(self)
        self._scan_control = control
        self.panel.show_running("Scan Progress:", len(entries), False, True, "Preparing…",
                                cancel_text="Cancel Scan, Use Cache")
        self.set_busy(True)
        self.backend.when_refresh_stopped(lambda: self._launch_scan(entries, control))

    def _launch_scan(self, entries: List[dict], control: ui_backend.SyncControl) -> None:
        if control.cancel_requested:  # cancelled while waiting for the launch-time scan to stop
            self._finish_scan_cancel()
            return
        self._scan_total = len(entries)
        self._scan_done = 0
        thread = self.backend.run(ui_backend.scan_task(self.config, entries, control,
                                                       self.apply_new_releases_call))  # v0.16.50
        thread.entryStarted.connect(self._on_scan_started)
        thread.entryDone.connect(self._on_scan_entry_done)
        thread.resultReady.connect(self._on_scan_finished)
        thread.cancelled.connect(self._on_scan_cancelled)
        thread.failed.connect(self._on_task_failed)

    def _on_scan_started(self, entry: dict) -> None:
        if self._discard_scan_updates:
            return
        self.on_entry_started(entry)
        self.panel.set_text(f"Scanning “{entry['folder_name']}”  |  Playlist {self._scan_done + 1}/{self._scan_total}")

    def _on_scan_entry_done(self, run) -> None:
        if self._discard_scan_updates:
            return
        self._scan_done += 1
        self.panel.set_bar(self._scan_done)
        self.on_entry_done(run)

    def _on_scan_cancelled(self, _partial: list) -> None:
        self._finish_scan_cancel()

    def _on_scan_finished(self, results: list) -> None:
        control, self._scan_control = self._scan_control, None
        if control is not None and control.cancel_requested:
            # Cancel was pressed just as the last playlist finished:
            # still honour it -- cached state, no results page.
            self._finish_scan_cancel()
            return
        self.set_busy(False)
        self.panel.hide_panel()
        self.refresh_status_card()
        self.openResults.emit("scan", results)

    def _start_integrity(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        self._running_kind = "integrity"
        self.panel.show_running("Integrity Check:", len(entries), False, False, "Checking files…")
        self.set_busy(True)
        self.backend.when_refresh_stopped(lambda: self._launch_integrity(entries))

    def _launch_integrity(self, entries: List[dict]) -> None:
        self._scan_total = len(entries)
        self._scan_done = 0
        thread = self.backend.run(ui_backend.integrity_task(self.config, entries))
        thread.entryStarted.connect(self._on_integrity_started)
        thread.entryDone.connect(self._on_integrity_entry_done)
        thread.resultReady.connect(self._on_integrity_finished)
        thread.failed.connect(self._on_task_failed)

    def _on_integrity_started(self, entry: dict) -> None:
        self.panel.set_text(f"Checking “{entry['folder_name']}”  |  Playlist {self._scan_done + 1}/{self._scan_total}")

    def _on_integrity_entry_done(self, run) -> None:
        self._scan_done += 1
        self.panel.set_bar(self._scan_done)

    # ---- Copy to Device (v0.26.0, Checklist item 66) ---------------------
    def _start_copy(self, entries: List[dict]) -> None:
        if not entries or self.backend.heavy_running or not self._need_library(entries):
            return
        from PySide6.QtWidgets import QFileDialog
        start = ui_settings.get("device_copy_last") or ""
        dest = QFileDialog.getExistingDirectory(
            self, "Choose the folder on the device (USB stick, SD card, phone shown as a drive)", start)
        if not dest:
            return
        sources = []
        for e in entries:
            try:
                sources.append(config_manager.get_playlist_folder(self.config, e))
            except ValueError:
                pass
        problem = device_copy.check_destination(dest, sources, config_manager.get_library_path(self.config))
        if problem:
            ui_dialogs.warn(self, "Copy to Device", problem)
            return
        ui_settings.set_value("device_copy_last", dest)
        self._running_kind = "copy"
        self.panel.show_running("Copy to Device:", 1, True, True, "Checking what to copy…")
        self.panel.start_clock()
        self.set_busy(True)
        self.backend.when_refresh_stopped(lambda: self._launch_copy(entries, dest))

    def _launch_copy(self, entries: List[dict], dest: str) -> None:
        self._control = ui_backend.SyncControl(self)
        self._control.stateChanged.connect(self.panel.set_control_state)
        thread = self.backend.run(ui_backend.device_copy_task(self.config, entries, dest, self._control))
        thread.progress.connect(self._on_copy_progress)
        thread.resultReady.connect(self._on_copy_finished)
        thread.cancelled.connect(self._on_copy_cancelled)
        thread.failed.connect(self._on_copy_failed)

    def _on_copy_progress(self, text: str) -> None:
        """'done/total|line' from device_copy_task."""
        counts, _, line = text.partition("|")
        try:
            done, total = (int(x) for x in counts.split("/"))
        except ValueError:
            return
        self.panel.set_bar(done / total if total else 1.0)
        self.panel.set_text(f"{line}  |  {done}/{total} files" if total else line)

    def _copy_ended(self) -> None:
        self._control = None
        self.set_busy(False)

    def _on_copy_finished(self, results: list) -> None:
        self._copy_ended()
        line, details = device_copy.summarize(results)
        self._last_sync_report = details
        self.panel.set_bar(1.0)
        errors = any(r.errors for r in results)
        self.panel.show_finished(line, details, auto_hide_ms=0 if errors else 15000, banner="Copy Complete")

    def _on_copy_cancelled(self, _partial) -> None:
        self._copy_ended()
        self.panel.show_finished("Copy cancelled. What was copied before stopping stays on the device; the next "
                                 "copy finishes the rest.", "")

    def _on_copy_failed(self, message: str) -> None:
        self._copy_ended()
        if message.startswith("DeviceCopyError: "):  # TaskThread prefixes the exception's type
            message = message[len("DeviceCopyError: "):]
        self.panel.show_finished(f"Copy to Device stopped: {message}", message)

    def _on_integrity_finished(self, results: list) -> None:
        self.set_busy(False)
        self.panel.hide_panel()
        self.openResults.emit("integrity", results)
