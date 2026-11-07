"""Timeline widgets: shared view state, ruler, track lanes, track headers, meters, sliders."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt, Signal, QObject, QRectF, QPointF, QLineF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QBrush
from PySide6.QtWidgets import (QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QSlider,
                               QToolButton, QComboBox, QSizePolicy, QMenu)

from .project import SR, Track

LANE_H = 104
HEADER_W = 250
PEAK_BIN = 64

BG = QColor("#16171c")
LANE_BG = QColor("#1d1f26")
LANE_SEL_BG = QColor("#252833")
GRID = QColor("#2a2d38")
TEXT_DIM = QColor("#7b8091")


def fmt_time(samples: int, ms=True) -> str:
    s = max(0, samples) / SR
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:06.3f}" if ms else f"{int(m)}:{int(sec):02d}"


# ---------------------------------------------------------------- shared state
class TimelineState(QObject):
    changed = Signal()
    view_changed = Signal()

    def __init__(self):
        super().__init__()
        self.length = SR * 10
        self.pps = 40.0                 # pixels per second
        self.offset = 0.0               # seconds at left edge
        self.lane_width = 800
        self.cursor = 0                 # samples
        self.sel: tuple[int, int] | None = None
        self.sel_track: str | None = None
        self.playhead: int | None = None
        self.loop = False

    def x_to_s(self, x: float) -> int:
        return int(round((self.offset + x / self.pps) * SR))

    def s_to_x(self, s: int) -> float:
        return (s / SR - self.offset) * self.pps

    def visible_seconds(self) -> float:
        return self.lane_width / self.pps

    def set_offset(self, sec: float):
        maxoff = max(0.0, self.length / SR - self.visible_seconds() * 0.9)
        self.offset = min(max(0.0, sec), maxoff)
        self.view_changed.emit()
        self.changed.emit()

    def zoom(self, factor: float, anchor_x: float | None = None):
        if anchor_x is None:
            anchor_x = self.lane_width / 2
        anchor_t = self.offset + anchor_x / self.pps
        min_pps = max(2.0, self.lane_width / max(1.0, self.length / SR))
        self.pps = float(np.clip(self.pps * factor, min(min_pps, 20.0), 1500.0))
        self.set_offset(anchor_t - anchor_x / self.pps)

    def zoom_fit(self):
        self.pps = max(2.0, self.lane_width / max(1.0, self.length / SR))
        self.set_offset(0)


# ---------------------------------------------------------------- peaks
class PeakCache:
    def __init__(self):
        self._c: dict[str, tuple[int, np.ndarray]] = {}

    def get(self, t: Track) -> np.ndarray | None:
        if t.audio is None:
            return None
        hit = self._c.get(t.id)
        if hit and hit[0] == id(t.audio):
            return hit[1]
        a = t.audio
        mono = np.abs(a).max(axis=1)
        n = len(mono) // PEAK_BIN
        pk = mono[: n * PEAK_BIN].reshape(n, PEAK_BIN).max(axis=1)
        pk = np.append(pk, 0.0).astype(np.float32)   # sentinel for reduceat
        self._c[t.id] = (id(a), pk)
        return pk


def column_peaks(pk: np.ndarray, start_sample_f: float, samples_per_px: float, width: int, first_sample=0):
    xs = np.arange(width + 1)
    idx = ((start_sample_f - first_sample + xs * samples_per_px) / PEAK_BIN).astype(np.int64)
    last = len(pk) - 1
    idx = np.clip(idx, 0, last)
    starts = idx[:-1]
    out = np.maximum.reduceat(pk, np.append(starts, last))[:-1] if width else np.zeros(0)
    return out, starts


# ---------------------------------------------------------------- ruler
class Ruler(QWidget):
    clicked = Signal(int)

    def __init__(self, state: TimelineState):
        super().__init__()
        self.state = state
        self.setFixedHeight(26)
        state.changed.connect(self.update)

    def paintEvent(self, _):
        st = self.state
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#121318"))
        span = st.visible_seconds()
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120) if s * st.pps >= 70), 300)
        t = math.floor(st.offset / step) * step
        p.setFont(QFont("Sans", 8))
        while t <= st.offset + span + step:
            x = st.s_to_x(int(t * SR))
            p.setPen(QColor("#4a4f5e"))
            p.drawLine(QPointF(x, 14), QPointF(x, 26))
            for k in range(1, 4):
                xx = x + k * step * st.pps / 4
                p.drawLine(QPointF(xx, 21), QPointF(xx, 26))
            p.setPen(TEXT_DIM)
            label = f"{int(t // 60)}:{t % 60:04.1f}" if step < 1 else f"{int(t // 60)}:{int(t % 60):02d}"
            p.drawText(QPointF(x + 3, 12), label)
            t += step
        end_x = st.s_to_x(st.length)
        if end_x < self.width():
            p.fillRect(QRectF(end_x, 0, self.width() - end_x, 26), QColor("#0c0d10"))
        if st.sel:
            a, b = st.s_to_x(st.sel[0]), st.s_to_x(st.sel[1])
            p.fillRect(QRectF(a, 20, b - a, 6), QColor(120, 160, 255, 160 if not st.loop else 230))
        _draw_markers(p, st, self.height())

    def mousePressEvent(self, e):
        self.clicked.emit(max(0, min(self.state.length, self.state.x_to_s(e.position().x()))))

    def wheelEvent(self, e):
        _wheel(self.state, e)


def _draw_markers(p: QPainter, st: TimelineState, h: int):
    x = st.s_to_x(st.cursor)
    p.setPen(QPen(QColor(255, 255, 255, 170), 1, Qt.DashLine))
    p.drawLine(QPointF(x, 0), QPointF(x, h))
    if st.playhead is not None:
        x = st.s_to_x(st.playhead)
        p.setPen(QPen(QColor("#ffd166"), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))


def _wheel(st: TimelineState, e) -> bool:
    d = e.angleDelta()
    if e.modifiers() & Qt.ControlModifier:
        st.zoom(1.2 if d.y() > 0 else 1 / 1.2, e.position().x())
        return True
    dx = d.x() or (d.y() if e.modifiers() & Qt.ShiftModifier else 0)
    if dx:
        st.set_offset(st.offset - dx / 120 * st.visible_seconds() * 0.15)
        return True
    return False


# ---------------------------------------------------------------- lane
class Lane(QWidget):
    pressed = Signal(str)                 # track id
    context = Signal(str, object)         # track id, global pos

    def __init__(self, track: Track, state: TimelineState, peaks: PeakCache, engine_ref):
        super().__init__()
        self.track = track
        self.state = state
        self.peaks = peaks
        self.engine_ref = engine_ref     # callable -> (engine, rec_track_id, live_peaks, latency)
        self.setFixedHeight(LANE_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(False)
        self._drag_from: int | None = None
        state.changed.connect(self.update)

    def paintEvent(self, _):
        st, t = self.state, self.track
        p = QPainter(self)
        w, h = self.width(), self.height()
        selected = st.sel_track == t.id
        p.fillRect(self.rect(), LANE_SEL_BG if selected else LANE_BG)
        end_x = st.s_to_x(st.length)
        if end_x < w:
            p.fillRect(QRectF(end_x, 0, w - end_x, h), BG)
        p.setPen(GRID)
        p.drawLine(0, h - 1, w, h - 1)

        if st.sel and selected:
            a, b = st.s_to_x(st.sel[0]), st.s_to_x(st.sel[1])
            p.fillRect(QRectF(a, 0, b - a, h - 1), QColor(110, 150, 255, 55))
            p.setPen(QColor(110, 150, 255, 160))
            p.drawLine(QPointF(a, 0), QPointF(a, h))
            p.drawLine(QPointF(b, 0), QPointF(b, h))

        color = QColor(t.color)
        if t.mute:
            color.setAlpha(70)
        mid = h / 2
        amp = h / 2 - 6
        pk = self.peaks.get(t)
        off_px = t.offset_ms / 1000 * st.pps
        if pk is not None:
            spp = SR / st.pps
            cols, _ = column_peaks(pk, st.offset * SR - off_px * spp, spp, w)
            ext = min(w, int(end_x))
            self._draw_wave(p, cols[:max(0, ext)], mid, amp, color)
        else:
            p.setPen(TEXT_DIM)
            p.setFont(QFont("Sans", 9))
            msg = "Empty track - select it and press R to record" if t.kind == "audio" else "No audio in video"
            p.drawText(QRectF(12, 0, w, h), Qt.AlignVCenter | Qt.AlignLeft, msg)

        eng, rec_tid, live, lat = self.engine_ref()
        if eng is not None and eng.recording and rec_tid == t.id and live is not None and len(live):
            # live take: blocks start at rec_first_pos, shifted back by latency
            start_s = eng.rec_first_pos - lat
            spp = SR / st.pps
            a = int(max(0, st.s_to_x(max(start_s, 0))))
            cols, starts = column_peaks(np.append(live, 0).astype(np.float32),
                                        st.offset * SR, spp, w, first_sample=start_s)
            xs = np.arange(w)
            sx = st.offset * SR + xs * spp
            valid = (sx >= start_s) & (sx < start_s + len(live) * PEAK_BIN)
            cols = np.where(valid, cols, 0)
            p.fillRect(QRectF(a, 0, max(0, st.s_to_x(eng.pos - lat) - a), h - 1), QColor(255, 60, 60, 35))
            self._draw_wave(p, cols, mid, amp, QColor("#ff5c5c"))

        p.setPen(QColor(255, 255, 255, 60))
        p.setFont(QFont("Sans", 8))
        if t.offset_ms:
            p.drawText(QPointF(6, h - 8), f"offset {t.offset_ms:+.0f} ms")
        _draw_markers(p, st, h)

    @staticmethod
    def _draw_wave(p: QPainter, cols: np.ndarray, mid: float, amp: float, color: QColor):
        if not len(cols):
            return
        disp = np.minimum(1.0, cols) ** 0.75 * amp       # slight curve so quiet parts are visible
        p.setPen(QPen(color, 1))
        lines = [QLineF(x + 0.5, mid - d, x + 0.5, mid + d) for x, d in enumerate(disp) if d > 0.4]
        if lines:
            p.drawLines(lines)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        p.drawLine(QPointF(0, mid), QPointF(len(cols), mid))

    # interactions
    def mousePressEvent(self, e):
        st = self.state
        if e.button() == Qt.RightButton:
            self.pressed.emit(self.track.id)
            self.context.emit(self.track.id, e.globalPosition().toPoint())
            return
        s = max(0, min(st.length, st.x_to_s(e.position().x())))
        if e.modifiers() & Qt.ShiftModifier and st.sel_track == self.track.id:
            anchor = st.sel[0] if st.sel else st.cursor
            a, b = sorted((anchor, s))
            st.sel = (a, b)
            self._drag_from = anchor
        else:
            self._drag_from = s
            st.cursor = s
            st.sel = None
        self.pressed.emit(self.track.id)
        st.changed.emit()

    def mouseMoveEvent(self, e):
        if self._drag_from is None:
            return
        st = self.state
        s = max(0, min(st.length, st.x_to_s(e.position().x())))
        a, b = sorted((self._drag_from, s))
        if st.s_to_x(b) - st.s_to_x(a) >= 3:
            st.sel = (a, b)
            st.cursor = a
        st.changed.emit()

    def mouseReleaseEvent(self, e):
        self._drag_from = None

    def mouseDoubleClickEvent(self, e):
        self.state.sel = (0, self.state.length)
        self.state.cursor = 0
        self.state.changed.emit()

    def wheelEvent(self, e):
        if not _wheel(self.state, e):
            e.ignore()


# ---------------------------------------------------------------- header
def _nofocus(*ws):
    for w in ws:
        w.setFocusPolicy(Qt.NoFocus)


class ParamSlider(QWidget):
    """Label + slider + value readout bound to a float."""
    valueChanged = Signal(float)

    def __init__(self, name: str, lo: float, hi: float, value: float, step: float = 0.1,
                 fmt="{:.1f}", label_w=58, value_w=52, default: float | None = None):
        super().__init__()
        self.lo, self.step, self.fmt = lo, step, fmt
        self.default = value if default is None else default
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.name = QLabel(name)
        self.name.setFixedWidth(label_w)
        self.name.setStyleSheet("color:#9aa0b0")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, int(round((hi - lo) / step)))
        self.slider.setValue(int(round((value - lo) / step)))
        self.val = QLabel()
        self.val.setFixedWidth(value_w)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _nofocus(self.slider)
        lay.addWidget(self.name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.val)
        self.slider.valueChanged.connect(self._changed)
        self._show()
        self.setToolTip("Double-click the slider to reset")
        self.slider.mouseDoubleClickEvent = lambda e: self.setValue(self.default, emit=True)

    def value(self) -> float:
        return self.lo + self.slider.value() * self.step

    def setValue(self, v: float, emit=False):
        self.slider.blockSignals(not emit)
        self.slider.setValue(int(round((v - self.lo) / self.step)))
        self.slider.blockSignals(False)
        self._show()

    def _show(self):
        v = self.value()
        self.val.setText(self.fmt(v) if callable(self.fmt) else self.fmt.format(v))

    def _changed(self, _):
        self._show()
        self.valueChanged.emit(self.value())


def pan_text(v: float) -> str:
    if abs(v) < 0.01:
        return "C"
    return f"{abs(v) * 100:.0f}{'L' if v < 0 else 'R'}"


class TrackHeader(QFrame):
    selected = Signal(str)
    changed = Signal(str, str)            # track id, what ("mix" | "name" | "input" | "delete")

    def __init__(self, track: Track, input_names: list[str]):
        super().__init__()
        self.track = track
        self.setFixedSize(HEADER_W, LANE_H)
        self.setObjectName("trackHeader")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 5, 6, 4)
        lay.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.name = QToolButton()
        self.name.setText(track.name)
        self.name.setToolTip("Click to rename")
        self.name.setStyleSheet("QToolButton{border:none;background:transparent;font-weight:600;color:#e8eaf0;text-align:left}")
        self.name.clicked.connect(lambda: self.changed.emit(track.id, "name"))
        self.mute = QToolButton(text="M", checkable=True, checked=track.mute)
        self.solo = QToolButton(text="S", checkable=True, checked=track.solo)
        self.mute.setObjectName("muteBtn")
        self.solo.setObjectName("soloBtn")
        self.mute.setToolTip("Mute (M)")
        self.solo.setToolTip("Solo (S)")
        for b in (self.mute, self.solo):
            b.setFixedSize(24, 22)
        self.mute.toggled.connect(self._mute)
        self.solo.toggled.connect(self._solo)
        row.addWidget(self.name, 1)
        row.addWidget(self.mute)
        row.addWidget(self.solo)
        if track.kind == "audio":
            dl = QToolButton(text="✕")
            dl.setStyleSheet("padding:0px")
            dl.setToolTip("Delete track")
            dl.setFixedSize(22, 22)
            dl.clicked.connect(lambda: self.changed.emit(track.id, "delete"))
            _nofocus(dl)
            row.addWidget(dl)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(4)
        if track.kind == "audio":
            self.input = QComboBox()
            self.set_inputs(input_names)
            self.input.currentIndexChanged.connect(self._input)
            self.input.setToolTip("Which input this track records from")
            _nofocus(self.input)
            row2.addWidget(self.input, 1)
        else:
            lbl = QLabel("Video audio")
            lbl.setStyleSheet("color:#9aa0b0")
            row2.addWidget(lbl, 1)
        lay.addLayout(row2)

        self.vol = ParamSlider("Vol", -40, 12, track.volume_db, 0.5,
                               fmt=lambda v: "-inf" if v <= -40 else f"{v:+.1f} dB", label_w=26, value_w=56,
                               default=0.0)
        self.pan = ParamSlider("Pan", -1, 1, track.pan, 0.02, fmt=pan_text, label_w=26, value_w=56, default=0.0)
        self.vol.valueChanged.connect(self._vol)
        self.pan.valueChanged.connect(self._pan)
        lay.addWidget(self.vol)
        lay.addWidget(self.pan)
        _nofocus(self.name, self.mute, self.solo)
        self.set_selected(False)

    def set_inputs(self, names: list[str]):
        if self.track.kind != "audio":
            return
        self.input.blockSignals(True)
        self.input.clear()
        items = names or ["(no input device)"]
        self.input.addItems([f"In {i + 1}: {n}" if names else n for i, n in enumerate(items)])
        self.input.setCurrentIndex(min(self.track.input_channel, len(items) - 1))
        self.input.blockSignals(False)

    def set_selected(self, on: bool):
        c = self.track.color
        self.setStyleSheet(
            f"#trackHeader{{background:{'#2a2e3b' if on else '#20222a'};"
            f"border-left:4px solid {c};border-bottom:1px solid #2a2d38;}}")

    def refresh(self):
        self.name.setText(self.track.name)
        for w, v in ((self.mute, self.track.mute), (self.solo, self.track.solo)):
            w.blockSignals(True)
            w.setChecked(v)
            w.blockSignals(False)
        self.vol.setValue(self.track.volume_db)
        self.pan.setValue(self.track.pan)

    def mousePressEvent(self, e):
        self.selected.emit(self.track.id)

    def _mute(self, v):
        self.track.mute = v
        self.changed.emit(self.track.id, "mix")

    def _solo(self, v):
        self.track.solo = v
        self.changed.emit(self.track.id, "mix")

    def _vol(self, v):
        self.track.volume_db = -120.0 if v <= -40 else v
        self.changed.emit(self.track.id, "mix")

    def _pan(self, v):
        self.track.pan = v
        self.changed.emit(self.track.id, "mix")

    def _input(self, i):
        self.track.input_channel = max(0, i)
        self.changed.emit(self.track.id, "input")


# ---------------------------------------------------------------- meter
class Meter(QWidget):
    def __init__(self, label: str, width=120):
        super().__init__()
        self.label = label
        self.level = 0.0
        self.hold = 0.0
        self.clip = False
        self.setFixedSize(width, 22)
        self.setToolTip(f"{label} level (click to reset clip light)")

    def set_peak(self, peak: float):
        db = 20 * math.log10(max(peak, 1e-6))
        v = max(0.0, min(1.0, (db + 60) / 60))
        self.level = max(v, self.level * 0.85)
        self.hold = max(v, self.hold - 0.01)
        if peak >= 0.99:
            self.clip = True
        self.update()

    def mousePressEvent(self, e):
        self.clip = False
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#101115"))
        bw = w - 34
        p.setPen(TEXT_DIM)
        p.setFont(QFont("Sans", 7))
        p.drawText(QRectF(2, 0, 28, h), Qt.AlignVCenter, self.label)
        x0 = 30
        lw = self.level * bw
        grad_stops = [(0.75, QColor("#3ddc84")), (0.92, QColor("#ffd166")), (1.01, QColor("#ff5c5c"))]
        prev = 0.0
        for stop, col in grad_stops:
            seg = min(lw, stop * bw) - prev * bw
            if seg > 0:
                p.fillRect(QRectF(x0 + prev * bw, 5, seg, h - 10), col)
            prev = stop
        p.fillRect(QRectF(x0 + self.hold * bw - 1, 4, 2, h - 8), QColor("#e8eaf0"))
        p.fillRect(QRectF(w - 4, 4, 3, h - 8), QColor("#ff3b3b") if self.clip else QColor("#33363f"))
