"""Audio settings, export and help dialogs."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFormLayout, QComboBox, QDoubleSpinBox, QDialogButtonBox,
                               QLabel, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QFileDialog,
                               QRadioButton, QCheckBox, QButtonGroup)

from . import engine as eng
from .widgets import fmt_time


class AudioSettingsDialog(QDialog):
    def __init__(self, parent, host, in_name, out_name, blocksize, manual_ms, reported_ms):
        super().__init__(parent)
        self.setWindowTitle("Audio settings")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.host = QComboBox()
        self.host.addItems(eng.host_apis())
        self.host.setCurrentText(host)
        self.inp = QComboBox()
        self.out = QComboBox()
        self.block = QComboBox()
        for b in (256, 512, 1024, 2048):
            self.block.addItem(f"{b} samples ({b / 48:.0f} ms)", b)
        self.block.setCurrentIndex(max(0, self.block.findData(blocksize)))
        self.manual = QDoubleSpinBox()
        self.manual.setRange(-500, 500)
        self.manual.setSuffix(" ms")
        self.manual.setDecimals(1)
        self.manual.setValue(manual_ms)
        form.addRow("Audio system", self.host)
        form.addRow("Input (record from)", self.inp)
        form.addRow("Output (listen on)", self.out)
        form.addRow("Buffer size", self.block)
        form.addRow("Extra latency fix", self.manual)
        hint = QLabel(
            f"Reported round-trip latency: <b>{reported_ms:.1f} ms</b>. New takes are shifted back by this "
            "automatically.<br>If your takes still land late, increase <i>Extra latency fix</i>; if they land "
            "early, decrease it. (Or nudge a single track with Alt+←/→.)<br><br>"
            "<span style='color:#7b8091'>Tip: 'JACK Audio Connection Kit' is PipeWire on Fedora, and lists "
            "each device by name. Turn on <b>Direct Monitor</b> on the Scarlett to hear yourself with no "
            "delay.</span>")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        form.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.host.currentTextChanged.connect(lambda h: self._fill(h, None, None))
        self._fill(host, in_name, out_name)

    def _fill(self, host, in_name, out_name):
        g_in, g_out = eng.guess_devices(host)
        for combo, kind, want, guess in ((self.inp, "input", in_name, g_in), (self.out, "output", out_name, g_out)):
            combo.clear()
            if kind == "input":
                combo.addItem("(none - playback only)", None)
            for i, n, ch in eng.list_devices(host, kind):
                combo.addItem(f"{n}  ({ch} ch)", n)
            idx = combo.findData(want or guess)
            combo.setCurrentIndex(idx if idx >= 0 else min(1, combo.count() - 1))

    def values(self):
        return (self.host.currentText(), self.inp.currentData(), self.out.currentData(),
                self.block.currentData(), self.manual.value())


class ExportDialog(QDialog):
    def __init__(self, parent, default_path: str, sel: tuple[int, int] | None):
        super().__init__(parent)
        self.setWindowTitle("Export video")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.path = QLineEdit(default_path)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        v.addWidget(QLabel("Save as:"))
        v.addLayout(row)
        self.whole = QRadioButton("Whole video (picture copied untouched, full original quality)")
        self.part = QRadioButton(
            f"Only the selection {fmt_time(sel[0])} - {fmt_time(sel[1])} (video gets re-encoded)" if sel
            else "Only the selection (select a range on a track first)")
        self.whole.setChecked(True)
        self.part.setEnabled(sel is not None)
        grp = QButtonGroup(self)
        grp.addButton(self.whole)
        grp.addButton(self.part)
        v.addWidget(self.whole)
        v.addWidget(self.part)
        self.normalize = QCheckBox("Normalise loudness peak to -1 dB")
        self.normalize.setChecked(True)
        v.addWidget(self.normalize)
        self.bitrate = QComboBox()
        self.bitrate.addItems(["320k", "256k", "192k"])
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Audio quality (AAC):"))
        r2.addWidget(self.bitrate)
        r2.addStretch(1)
        v.addLayout(r2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export video", self.path.text(), "MP4 video (*.mp4)")
        if p:
            if not p.lower().endswith(".mp4"):
                p += ".mp4"
            self.path.setText(p)


SHORTCUTS = [
    ("Space", "Play / stop. Starts at the selection (or the cursor) and returns there when stopped"),
    ("R", "Record on the selected track (overwrites what's there). Press again or Space to stop"),
    ("", "With a selection, R punches in: 2 s pre-roll, records only inside the selection"),
    ("Esc", "Cancel a recording (throws the take away) / clear selection"),
    ("L", "Loop the selection while playing"),
    ("Home / End", "Cursor to start / end"),
    ("← / →", "Move cursor 1 s (Shift: 5 s)"),
    ("Alt+← / Alt+→", "Nudge selected track 5 ms earlier / later (Shift: 1 ms)"),
    ("↑ / ↓", "Select previous / next track"),
    ("M / S", "Mute / solo selected track"),
    ("Delete", "Silence the selection"),
    ("Ctrl+A", "Select the whole track"),
    ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo"),
    ("Ctrl+T", "Add track"),
    ("Ctrl+wheel, Ctrl+= / Ctrl+-", "Zoom    (Ctrl+0: fit)"),
    ("Shift+wheel", "Scroll timeline"),
    ("Shift+click", "Extend selection"),
    ("Double-click track", "Select the whole track"),
    ("Ctrl+S / Ctrl+E", "Save / export video"),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        v = QVBoxLayout(self)
        rows = "".join(f"<tr><td style='padding:3px 16px 3px 0'><b>{k}</b></td><td>{d}</td></tr>"
                       for k, d in SHORTCUTS)
        lbl = QLabel(f"<table>{rows}</table>")
        lbl.setTextFormat(Qt.RichText)
        v.addWidget(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
