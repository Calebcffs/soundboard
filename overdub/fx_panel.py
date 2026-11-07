"""Effects panel for the selected track (or the master bus)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, QGroupBox, QComboBox,
                               QScrollArea, QFrame, QPushButton)

from .project import FxSettings
from .fx import REVERB_TYPES
from .widgets import ParamSlider, _nofocus

# A few starting points; they only set values, everything stays tweakable.
PRESETS = {
    "Clean": {},
    "Vocal": dict(gate_on=False, eq_on=True, low_cut=90, low_gain=-1.5, mid_gain=1.5, mid_freq=3000,
                  high_gain=2.0, comp_on=True, comp_threshold=-20, comp_ratio=3.5, comp_makeup=3,
                  reverb_on=True, reverb_type="Plate", reverb_mix=0.22),
    "Bass (DI)": dict(gate_on=True, gate_threshold=-55, eq_on=True, low_cut=35, low_gain=2.0,
                      mid_gain=-2.0, mid_freq=400, high_gain=0.0, comp_on=True, comp_threshold=-22,
                      comp_ratio=4, comp_makeup=4, reverb_on=False),
    "Acoustic guitar": dict(eq_on=True, low_cut=80, low_gain=-2.0, mid_gain=-1.0, mid_freq=250,
                            high_gain=2.5, comp_on=True, comp_threshold=-18, comp_ratio=2.5,
                            comp_makeup=2, reverb_on=True, reverb_type="Room", reverb_mix=0.2),
    "Phone audio cleanup": dict(gate_on=False, eq_on=True, low_cut=70, low_gain=-2.0, mid_gain=-2.0,
                                mid_freq=500, high_gain=1.5, comp_on=True, comp_threshold=-20,
                                comp_ratio=2.5, comp_makeup=2, reverb_on=True, reverb_type="Room",
                                reverb_mix=0.15),
}


class FxPanel(QWidget):
    changed = Signal()          # any parameter changed

    def __init__(self):
        super().__init__()
        self.fx: FxSettings | None = None
        self.is_master = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        top = QHBoxLayout()
        self.title = QLabel("Effects")
        self.title.setStyleSheet("font-size:14px;font-weight:600")
        top.addWidget(self.title)
        top.addStretch(1)
        self.preset = QComboBox()
        self.preset.addItem("Preset...")
        self.preset.addItems(list(PRESETS))
        self.preset.activated.connect(self._apply_preset)
        _nofocus(self.preset)
        top.addWidget(self.preset)
        outer.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        outer.addWidget(self.scroll, 1)
        self.set_target(None, "")

    def set_target(self, fx: FxSettings | None, name: str, is_master=False):
        self.fx = fx
        self.is_master = is_master
        self.title.setText(f"Effects  ·  {name}" if fx else "Effects")
        self.preset.setVisible(fx is not None and not is_master)
        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        if fx is None:
            lay.addWidget(QLabel("Select a track to edit its effects."))
        else:
            if not is_master:
                lay.addWidget(self._group("Noise gate", "gate_on", [
                    ("Threshold", "gate_threshold", -80, -10, 1, "{:.0f} dB")]))
                lay.addWidget(self._group("EQ", "eq_on", [
                    ("Low cut", "low_cut", 20, 300, 1, lambda v: "off" if v <= 21 else f"{v:.0f} Hz"),
                    ("Bass", "low_gain", -12, 12, 0.5, "{:+.1f} dB"),
                    ("Mid", "mid_gain", -12, 12, 0.5, "{:+.1f} dB"),
                    ("Mid freq", "mid_freq", 200, 5000, 10, lambda v: f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"),
                    ("Treble", "high_gain", -12, 12, 0.5, "{:+.1f} dB")]))
            lay.addWidget(self._group("Compressor", "comp_on", [
                ("Threshold", "comp_threshold", -50, 0, 0.5, "{:.1f} dB"),
                ("Ratio", "comp_ratio", 1, 12, 0.1, "{:.1f}:1"),
                ("Makeup", "comp_makeup", 0, 18, 0.5, "{:+.1f} dB")]))
            lay.addWidget(self._group("Reverb", "reverb_on", [
                ("Mix", "reverb_mix", 0, 1, 0.01, lambda v: f"{v * 100:.0f}%")], reverb=True))
            if is_master:
                lay.addWidget(self._group("Limiter", "limiter_on", [
                    ("Ceiling", "limiter_threshold", -12, 0, 0.1, "{:.1f} dB")]))
                note = QLabel("The master bus is applied to the\nwhole mix, on top of each track's\neffects. "
                              "The limiter stops\nthe export from clipping.")
                note.setStyleSheet("color:#7b8091")
                note.setAlignment(Qt.AlignTop)
                lay.addWidget(note)
        lay.addStretch(1)
        self.scroll.setWidget(body)

    def _group(self, title, on_attr, params, reverb=False) -> QGroupBox:
        fx = self.fx
        g = QGroupBox(title)
        g.setCheckable(True)
        g.setChecked(getattr(fx, on_attr))
        g.setFocusPolicy(Qt.NoFocus)
        g.setMinimumWidth(225)
        v = QVBoxLayout(g)
        v.setSpacing(3)
        if reverb:
            row = QHBoxLayout()
            lbl = QLabel("Type")
            lbl.setFixedWidth(58)
            lbl.setStyleSheet("color:#9aa0b0")
            cb = QComboBox()
            cb.addItems(list(REVERB_TYPES))
            cb.setCurrentText(fx.reverb_type)
            _nofocus(cb)
            cb.currentTextChanged.connect(lambda s: self._set("reverb_type", s))
            row.addWidget(lbl)
            row.addWidget(cb, 1)
            v.addLayout(row)
        for label, attr, lo, hi, step, fmt in params:
            s = ParamSlider(label, lo, hi, getattr(fx, attr), step, fmt,
                            default=getattr(FxSettings(), attr))
            s.valueChanged.connect(lambda val, a=attr: self._set(a, val))
            v.addWidget(s)
        v.addStretch(1)
        g.toggled.connect(lambda on: self._set(on_attr, on))
        return g

    def _set(self, attr, value):
        if self.fx is not None:
            setattr(self.fx, attr, value)
            self.changed.emit()

    def _apply_preset(self, idx):
        name = self.preset.itemText(idx)
        self.preset.setCurrentIndex(0)
        if self.fx is None or name not in PRESETS:
            return
        fresh = FxSettings(limiter_on=False)
        for k, val in {**fresh.__dict__, **PRESETS[name]}.items():
            setattr(self.fx, k, val)
        self.set_target(self.fx, self.title.text().split("·")[-1].strip())
        self.changed.emit()
