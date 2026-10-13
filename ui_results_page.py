"""
Mud Music Manager - Scan Results & Check Integrity Results subpages (v0.16.0)

One page, two modes. Each checked playlist gets a card:
  Scan      -> what a sync WOULD do (missing / no longer live / renumber)
  Integrity -> leftover temp files, unreadable files, files with no
               identity tag, with the same "remove leftover temp files"
               cleanup the terminal menu offers.
Both are read-only reports; nothing here changes a file except the
explicit cleanup button.

v0.16.13: shown in the in-app overlay over the current screen, with its
own close button in the title row.

v0.16.24: keyboard (Checklist item 27 part 3, handled by ui_keys): Space
or Esc closes; W/A/S/D reach "Remove leftover files" (never highlighted
on opening) and W/S scroll long results.
"""

from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

import config_manager
import library_sync
import track_failures
import ui_backend
import ui_dialogs
from ui_widgets import CapsLabel, GlyphButton, PillLabel, make_button, make_panel

MAX_LINES = 12


class ResultsPage(QWidget):
    closeRequested = Signal()
    changed = Signal()   # something was cleaned up -> main page may refresh

    # Keyboard (v0.16.24): Space with nothing highlighted closes the page,
    # and the first move key lands on the close button.
    KEY_SPACE_CLOSES = True

    def key_default(self):
        return self.close_btn

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.kind = "scan"
        self.close_btn = GlyphButton("close", 36, boxed=True)
        self.close_btn.setToolTip("Close (Esc)")
        self.close_btn.clicked.connect(self.closeRequested.emit)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 20, 22, 22)
        outer.setSpacing(12)
        title_row = QHBoxLayout()
        tb = QVBoxLayout()
        tb.setSpacing(0)
        self.caps = CapsLabel("Results")
        tb.addWidget(self.caps)
        self.title = QLabel("Scan Results")
        self.title.setProperty("role", "title")
        tb.addWidget(self.title)
        self.summary = QLabel("")
        self.summary.setProperty("role", "muted")
        self.summary.setWordWrap(True)
        tb.addWidget(self.summary)
        title_row.addLayout(tb, 1)
        title_row.addWidget(self.close_btn, 0, Qt.AlignTop)
        outer.addLayout(title_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.col = QVBoxLayout(self.host)
        self.col.setContentsMargins(0, 0, 8, 0)
        self.col.setSpacing(14)
        self.col.addStretch(1)
        scroll.setWidget(self.host)
        outer.addWidget(scroll, 1)

    # ------------------------------------------------------------------
    def show_results(self, kind: str, runs: list) -> None:
        self.kind = kind
        self.title.setText("Scan Results" if kind == "scan" else "Check Integrity Results")
        self.caps.setText("Scan" if kind == "scan" else "Check integrity")
        while self.col.count() > 1:
            item = self.col.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        needs = 0
        for run in runs:
            card = self._scan_card(run) if kind == "scan" else self._integrity_card(run)
            self.col.insertWidget(self.col.count() - 1, card[0])
            needs += card[1]
        if kind == "scan":
            self.summary.setText(
                "Everything is in sync." if not needs else
                f"{needs} playlist{'s' if needs != 1 else ''} not fully in sync. Nothing was changed.")
        else:
            self.summary.setText(
                "No problems found in any checked playlist." if not needs else
                f"{needs} playlist{'s' if needs != 1 else ''} with file problems. Nothing was changed.")

    # ------------------------------------------------------------------
    def _card_shell(self, run, pill_text: str, tone: str):
        card = make_panel()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        name = QLabel(run.name)
        f = name.font()
        f.setPointSizeF(14)
        f.setBold(True)
        name.setFont(f)
        head.addWidget(name)
        head.addStretch(1)
        head.addWidget(PillLabel(pill_text, tone))
        lay.addLayout(head)
        return card, lay

    @staticmethod
    def _section(lay, title: str, lines: List[str], tone_role: str = "") -> None:
        if not lines:
            return
        h = QLabel(f"{title}  ({len(lines)})")
        hf = h.font()
        hf.setBold(True)
        h.setFont(hf)
        lay.addWidget(h)
        shown = lines[:MAX_LINES]
        body = QLabel("\n".join(f"•  {x}" for x in shown) +
                      (f"\n…and {len(lines) - MAX_LINES} more" if len(lines) > MAX_LINES else ""))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setProperty("role", tone_role or "muted")
        lay.addWidget(body)

    def _scan_card(self, run):
        if run.error:
            card, lay = self._card_shell(run, "Couldn't scan", "bad")
            err = QLabel(run.error)
            err.setWordWrap(True)
            err.setProperty("role", "muted")
            lay.addWidget(err)
            return card, 0
        r = run.result
        counts = ui_backend.scan_attention(r)
        pending = counts["pending"]
        red = ui_backend.red_text(counts["no_match"], counts["unavailable"])
        if red:
            pill_text, tone = (f"{pending} pending · {red}" if pending else red), "bad"
        else:
            pill_text, tone = ("In Sync", "ok") if not pending else (f"{pending} pending", "warn")
        card, lay = self._card_shell(run, pill_text, tone)
        info = QLabel(f"{r.total_live_tracks} tracks in the live playlist · {r.unchanged_count} unchanged")
        info.setProperty("role", "muted")
        lay.addWidget(info)

        # v0.16.27 (Checklist item 28): missing tracks split by what the
        # last sync found -- No match / Unavailable in red, the rest with
        # the reason they failed last time, if they did.
        no_match, unavailable, missing = [], [], []
        for t in r.missing:
            name = f"{getattr(t, 'primary_artist', '')} – {getattr(t, 'name', '')}".strip(" –")
            record = r.known_failures.get(track_failures.track_key(t))
            st = track_failures.status(record)
            if st == track_failures.STATUS_NO_MATCH:
                no_match.append(name)
            elif st == track_failures.STATUS_UNAVAILABLE:
                unavailable.append(f"{name} — {track_failures.label(record.get('kind'))}")
            elif record:
                failed = int(record.get("failed_syncs") or 0)
                missing.append(f"{name} — last sync: {track_failures.label(record.get('kind'))}"
                               f" ({failed} of {track_failures.UNAVAILABLE_AFTER})")
            else:
                missing.append(name)
        self._section(lay, "No match — needs an exception link (the search worked, nothing was the right length)",
                      no_match, "bad")
        self._section(lay, f"Unavailable — age-restricted, or failed on {track_failures.UNAVAILABLE_AFTER} or more "
                           "syncs (still tried on every sync)", unavailable, "bad")
        self._section(lay, "Missing locally — would be downloaded", missing)
        self._section(lay, "No longer in the playlist — would be removed", list(r.no_longer_live))
        self._section(lay, "Track number would change", list(r.needs_renumber))
        return card, 1 if (pending or red) else 0

    def _integrity_card(self, run):
        if run.error:
            card, lay = self._card_shell(run, "Couldn't check", "bad")
            err = QLabel(run.error)
            err.setWordWrap(True)
            err.setProperty("role", "muted")
            lay.addWidget(err)
            return card, 0
        r = run.result
        card, lay = self._card_shell(run, "Clean" if r.clean else "Needs attention", "ok" if r.clean else "warn")
        if r.clean:
            ok = QLabel("No leftover files, unreadable files or untagged files.")
            ok.setProperty("role", "muted")
            lay.addWidget(ok)
            return card, 0
        self._section(lay, "Leftover files from an interrupted download", [p.name for p in r.orphaned_temp_files])
        self._section(lay, "Files that couldn't be read (possibly corrupted)", list(r.unreadable_files))
        self._section(lay, "Files with no identity tag (skipped by every sync)", list(r.untagged_files))
        if r.orphaned_temp_files:
            btn = make_button("Remove leftover files", "danger", compact=True)
            btn.clicked.connect(lambda _c=False, rr=run, b=btn: self._cleanup(rr, b))
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(btn)
            lay.addLayout(row)
        return card, 1

    def _cleanup(self, run, btn) -> None:
        if not ui_dialogs.ask_yes_no(
                self, "Remove leftover files",
                f"Delete the {len(run.result.orphaned_temp_files)} leftover file(s) in “{run.name}”? "
                "Your finished songs are not touched.", "Remove", "Cancel"):
            return
        try:
            folder = config_manager.get_playlist_folder(self.ctx.config, run.entry)
            removed = library_sync.clean_orphaned_temp_files(folder)
        except Exception as e:
            ui_dialogs.warn(self, "Couldn't clean up", str(e))
            return
        btn.setEnabled(False)
        btn.setText(f"Removed {removed}")
        self.changed.emit()
