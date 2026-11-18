"""Main window: transport, effects panel, video preview, multitrack timeline."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt, QTimer, QSettings, QUrl, Signal, QObject
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QDesktopServices, QFont
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QScrollArea,
                               QScrollBar, QToolButton, QLabel, QFileDialog, QMessageBox, QInputDialog,
                               QProgressDialog, QStackedWidget, QPushButton, QListWidget, QMenu,
                               QFrame, QSizePolicy, QListWidgetItem)

from . import engine as eng
from . import media
from .dialogs import (AudioSettingsDialog, ExportDialog, ShortcutsDialog, LatencyDialog, ScratchDialog,
                      LengthDialog)
from .fx import render_mix
from .fx_panel import FxPanel
from .project import SR, Project, Track, TRACK_COLORS
from .widgets import (TimelineState, PeakCache, Ruler, Lane, TrackHeader, Meter, ParamSlider,
                      HEADER_W, PEAK_BIN, fmt_time, _nofocus)

PROJECTS_DIR = os.path.expanduser("~/Overdub")
VIDEO_EXTS = "Videos (*.mp4 *.mov *.MP4 *.MOV *.m4v *.mkv *.webm *.3gp)"


class Worker(QObject):
    """Run fn in a thread; fn gets a progress(msg, frac) callback."""
    progress = Signal(str, float)
    done = Signal(object)
    failed = Signal(str)

    def start(self, fn):
        def run():
            try:
                self.done.emit(fn(lambda m, f: self.progress.emit(m, f)))
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e))
        threading.Thread(target=run, daemon=True).start()
        return self


class MainWindow(QMainWindow):
    render_done = Signal(str)
    proxy_ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Overdub")
        self.resize(1400, 860)
        self.settings = QSettings("overdub", "overdub")
        self.project: Project | None = None
        self.engine = eng.AudioEngine()
        self.renderer = eng.Renderer(lambda tid: self.render_done.emit(tid))
        self.render_done.connect(self._on_render_done)
        self.proxy_ready.connect(self._set_video)
        self.state = TimelineState()
        self.peaks = PeakCache()
        self.headers: dict[str, TrackHeader] = {}
        self.lanes: dict[str, Lane] = {}
        self.play_origin = 0
        self.punch: tuple[int, int | None] = (0, None)
        self.rec_track_id: str | None = None
        self.live_peaks = np.zeros(0, np.float32)
        self._live_idx = 0
        self._live_tail = np.zeros(0, np.float32)
        self._last_video_seek = 0.0
        self._last_video_cursor = -1
        self._workers = []

        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._open_audio(initial=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self.autosave = QTimer(self)
        self.autosave.timeout.connect(self._autosave)
        self.autosave.start(20000)

    # ================================================================ UI
    def _build_ui(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_welcome())

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_transport())

        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)
        self.fx_panel = FxPanel()
        self.fx_panel.changed.connect(self._on_fx_changed)
        top.addWidget(self.fx_panel)
        vbox = QWidget()
        self.video_box = vbox
        vl = QVBoxLayout(vbox)
        vl.setContentsMargins(6, 6, 6, 6)
        self.video = QVideoWidget()
        self.video.setMinimumSize(260, 200)
        self.video.setStyleSheet("background:#000")
        self.video_msg = QLabel("")
        self.video_msg.setAlignment(Qt.AlignCenter)
        self.video_msg.setStyleSheet("color:#7b8091")
        vl.addWidget(self.video, 1)
        vl.addWidget(self.video_msg)
        top.addWidget(vbox)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 1)
        top.setSizes([1000, 400])
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        split.addWidget(top)
        split.addWidget(self._build_timeline())
        split.setSizes([330, 500])
        v.addWidget(split, 1)
        self.stack.addWidget(page)
        self.status = self.statusBar()
        self.dev_label = QLabel()
        self.dev_label.setStyleSheet("color:#7b8091")
        self.status.addPermanentWidget(self.dev_label)

    def _build_welcome(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.addStretch(1)
        box = QVBoxLayout()
        title = QLabel("Overdub")
        title.setStyleSheet("font-size:42px;font-weight:700;color:#e8eaf0")
        sub = QLabel("Pick a video from your phone (or start from nothing), record parts over it, mix, export.")
        sub.setStyleSheet("color:#9aa0b0;font-size:14px")
        b1 = QPushButton("New project from video...")
        b3 = QPushButton("Start from scratch (no video)...")
        b2 = QPushButton("Open project...")
        for b in (b1, b3, b2):
            b.setMinimumHeight(40)
            b.setStyleSheet("font-size:14px")
        b1.clicked.connect(self.new_project)
        b2.clicked.connect(self.open_project_dialog)
        b3.clicked.connect(self.new_scratch_project)
        self.recent = QListWidget()
        self.recent.setMaximumHeight(200)
        self.recent.itemActivated.connect(lambda it: self.load_project_folder(it.data(Qt.UserRole)))
        for x in (title, sub):
            box.addWidget(x, 0, Qt.AlignHCenter)
        box.addSpacing(20)
        box.addWidget(b1)
        box.addWidget(b3)
        box.addWidget(b2)
        box.addSpacing(16)
        self.recent_label = QLabel("Recent projects")
        self.recent_label.setStyleSheet("color:#9aa0b0")
        box.addWidget(self.recent_label)
        box.addWidget(self.recent)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addLayout(box)
        h.addStretch(1)
        outer.addLayout(h)
        outer.addStretch(2)
        self._fill_recent()
        return w

    def _build_transport(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("transport")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(6)

        def btn(text, tip, slot, checkable=False, name=None):
            b = QToolButton(text=text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setMinimumSize(38, 32)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            _nofocus(b)
            h.addWidget(b)
            return b

        btn("⏮", "Go to start (Home)", self.go_start)
        self.play_btn = btn("▶", "Play / stop (Space)", self.toggle_play, name="playBtn")
        self.rec_btn = btn("●", "Record on selected track (R)", self.toggle_record, name="recBtn")
        self.loop_btn = btn("⟲", "Loop selection (L)", self.toggle_loop, checkable=True)
        self.time_label = QLabel("00:00.000")
        self.time_label.setFont(QFont("Monospace", 15, QFont.Bold))
        self.time_label.setStyleSheet("color:#e8eaf0;padding:0 10px")
        self.time_label.setMinimumWidth(130)
        h.addWidget(self.time_label)
        self.sel_label = QLabel("")
        self.sel_label.setStyleSheet("color:#7b8091")
        h.addWidget(self.sel_label)
        h.addStretch(1)
        self.in_meter = Meter("IN")
        self.out_meter = Meter("OUT")
        h.addWidget(self.in_meter)
        h.addWidget(self.out_meter)
        self.master_vol = ParamSlider("Master", -24, 12, 0, 0.5, "{:+.1f} dB", label_w=44, value_w=56,
                                      default=0.0)
        self.master_vol.setFixedWidth(230)
        self.master_vol.valueChanged.connect(self._master_vol)
        h.addWidget(self.master_vol)
        self.master_btn = btn("Master FX", "Edit the effects on the whole mix", self.show_master_fx, checkable=True)
        btn("Audio...", "Input/output devices and latency", self.audio_settings)
        exp = btn("Export video", "Export the video with your mix (Ctrl+E)", self.export, name="exportBtn")
        exp.setMinimumWidth(110)
        self.export_btn = exp
        return bar

    def _build_timeline(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        rr = QHBoxLayout()
        rr.setSpacing(0)
        corner = QLabel("  Tracks")
        corner.setFixedWidth(HEADER_W)
        corner.setStyleSheet("background:#121318;color:#7b8091")
        self.ruler = Ruler(self.state)
        self.ruler.clicked.connect(self._ruler_click)
        rr.addWidget(corner)
        rr.addWidget(self.ruler, 1)
        v.addLayout(rr)

        self.track_area = QScrollArea()
        self.track_area.setWidgetResizable(True)
        self.track_area.setFrameShape(QFrame.NoFrame)
        self.track_area.setFocusPolicy(Qt.NoFocus)
        self.track_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.rows = QVBoxLayout(inner)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        self.rows.addStretch(1)
        self.track_area.setWidget(inner)
        v.addWidget(self.track_area, 1)

        bottom = QHBoxLayout()
        add = QPushButton("+ Add track")
        add.setFixedWidth(HEADER_W)
        add.setFocusPolicy(Qt.NoFocus)
        add.clicked.connect(lambda: self.add_track())
        self.hscroll = QScrollBar(Qt.Horizontal)
        self.hscroll.setFocusPolicy(Qt.NoFocus)
        self.hscroll.valueChanged.connect(lambda val: self.state.set_offset(val / 1000))
        self.state.view_changed.connect(self._sync_scrollbar)
        bottom.addWidget(add)
        bottom.addWidget(self.hscroll, 1)
        v.addLayout(bottom)
        return w

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self._act(f, "New project from video...", self.new_project, "Ctrl+N")
        self._act(f, "New project from scratch...", self.new_scratch_project, "Ctrl+Shift+N")
        self._act(f, "Open project...", self.open_project_dialog, "Ctrl+O")
        self._act(f, "Save", self.save, "Ctrl+S")
        f.addSeparator()
        self._act(f, "Export video...", self.export, "Ctrl+E")
        self._act(f, "Export mix as WAV...", self.export_wav)
        self._act(f, "Show project folder", self.show_folder)
        f.addSeparator()
        self._act(f, "Quit", self.close, "Ctrl+Q")
        e = mb.addMenu("&Edit")
        self._act(e, "Undo", self.undo, "Ctrl+Z")
        self._act(e, "Redo", self.redo, "Ctrl+Shift+Z")
        e.addSeparator()
        self._act(e, "Cut", self.cut, "Ctrl+X")
        self._act(e, "Copy", self.copy, "Ctrl+C")
        self._act(e, "Paste (overwrites at cursor)", self.paste, "Ctrl+V")
        e.addSeparator()
        self._act(e, "Selection louder (+1 dB)", lambda: self.gain_selection(1.0), "Shift+Up")
        self._act(e, "Selection quieter (-1 dB)", lambda: self.gain_selection(-1.0), "Shift+Down")
        self._act(e, "Change selection level...", self.gain_selection_dialog, "G")
        self._act(e, "Silence selection", self.silence_selection)
        self._act(e, "Fade in selection", lambda: self.fade_selection(True))
        self._act(e, "Fade out selection", lambda: self.fade_selection(False))
        self._act(e, "Normalise selection", self.normalize_selection)
        e.addSeparator()
        self.length_act = self._act(e, "Change length...", self.change_length)
        self.length_act.setEnabled(False)
        t = mb.addMenu("&Track")
        self._act(t, "Add track", lambda: self.add_track(), "Ctrl+T")
        self._act(t, "Add bass track (input 2)", lambda: self.add_track("Bass", 1))
        self._act(t, "Add vocal track (input 1)", lambda: self.add_track("Vocals", 0))
        self._act(t, "Rename selected track...", lambda: self._rename(self.state.sel_track))
        self._act(t, "Delete selected track", lambda: self._delete(self.state.sel_track))
        a = mb.addMenu("&Audio")
        self._act(a, "Audio settings...", self.audio_settings)
        self._act(a, "Measure latency...", self.measure_latency)
        self._act(a, "Master effects", self.show_master_fx)
        h = mb.addMenu("&Help")
        self._act(h, "Keyboard shortcuts", lambda: ShortcutsDialog(self).exec(), "F1")

    def _act(self, menu, text, slot, key=None):
        a = QAction(text, self)
        if key:
            a.setShortcut(QKeySequence(key))
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.WindowShortcut)
            s.activated.connect(lambda: fn() if self.project else None)
        sc("Space", self.toggle_play)
        sc("R", self.toggle_record)
        sc("L", lambda: (self.loop_btn.toggle(), self.toggle_loop()))
        sc("Escape", self.escape)
        sc("Home", self.go_start)
        sc("End", lambda: self.set_cursor(self.project.length))
        sc("Left", lambda: self.move_cursor(-1))
        sc("Right", lambda: self.move_cursor(1))
        sc("Shift+Left", lambda: self.move_cursor(-5))
        sc("Shift+Right", lambda: self.move_cursor(5))
        sc("Alt+Left", lambda: self.nudge(-5))
        sc("Alt+Right", lambda: self.nudge(5))
        sc("Alt+Shift+Left", lambda: self.nudge(-1))
        sc("Alt+Shift+Right", lambda: self.nudge(1))
        sc("Up", lambda: self.select_relative(-1))
        sc("Down", lambda: self.select_relative(1))
        sc("M", lambda: self._toggle_flag("mute"))
        sc("S", lambda: self._toggle_flag("solo"))
        sc("Delete", self.silence_selection)
        sc("Backspace", self.silence_selection)
        sc("Ctrl+A", self.select_all)
        sc("Ctrl+Y", self.redo)
        sc("Ctrl+=", lambda: self.state.zoom(1.5))
        sc("Ctrl++", lambda: self.state.zoom(1.5))
        sc("Ctrl+-", lambda: self.state.zoom(1 / 1.5))
        sc("Ctrl+0", self.state.zoom_fit)

    # ================================================================ audio devices
    def _open_audio(self, initial=False):
        host = self.settings.value("host") or eng.preferred_host()
        if host not in eng.host_apis():
            host = eng.preferred_host()
        g_in, g_out = eng.guess_devices(host)
        in_name = self.settings.value("input", g_in)
        out_name = self.settings.value("output", g_out)
        if in_name == "__none__":
            in_name = None
        block = int(self.settings.value("blocksize", 1024))
        self.engine.manual_latency_ms = float(self.settings.value("manual_latency_ms", 0.0))
        in_idx = eng.device_index_by_name(host, "input", in_name)
        out_idx = eng.device_index_by_name(host, "output", out_name)
        self.in_device_name = in_name if in_idx is not None else None
        self._latency_key = f"{host}|{in_name}|{out_name}|{block}"
        self.engine.measured_latency_ms = self._measured_map().get(self._latency_key)
        try:
            self.engine.open(in_idx, out_idx, block)
            self._update_dev_label(out_name)
        except Exception as e:  # noqa: BLE001
            self.dev_label.setText("Audio device not open")
            msg = f"Couldn't open the audio device:\n{e}\n\nPick another input/output in Audio settings."
            if initial:
                QTimer.singleShot(300, lambda: (QMessageBox.warning(self, "Audio", msg), self.audio_settings()))
            else:
                QMessageBox.warning(self, "Audio", msg)
        self._refresh_input_names()

    def _update_dev_label(self, out_name=None):
        out_name = out_name or self.settings.value("output", "")
        e = self.engine
        how = "measured" if e.measured_latency_ms is not None else "estimated - Audio > Measure latency"
        extra = f" {e.manual_latency_ms:+.0f}" if e.manual_latency_ms else ""
        self.dev_label.setText(f"In: {self.in_device_name or 'none'}   Out: {out_name}   "
                               f"latency {e.base_latency_ms():.1f} ms{extra} ({how})")

    def _measured_map(self) -> dict:
        try:
            return json.loads(self.settings.value("measured_latency", "{}") or "{}")
        except (TypeError, ValueError):
            return {}

    def measure_latency(self):
        self.stop_all()
        if self.engine.stream is None or not self.engine.in_channels:
            QMessageBox.information(self, "Measure latency", "Open an input and output device first (Audio settings).")
            return
        d = LatencyDialog(self, self.engine, self._input_names(), self.engine.base_latency_ms(),
                          self.engine.measured_latency_ms is not None)
        if d.exec() and d.result_ms is not None:
            m = self._measured_map()
            m[self._latency_key] = round(d.result_ms, 2)
            self.settings.setValue("measured_latency", json.dumps(m))
            self.engine.measured_latency_ms = d.result_ms
            self._update_dev_label()
            self.status.showMessage(f"Latency set to {d.result_ms:.1f} ms. New takes will line up to that.", 6000)

    def _input_names(self) -> list[str]:
        n = self.engine.in_channels
        if not n:
            return []
        name = self.in_device_name or ""
        if "Scarlett Solo" in name and n == 2 and "Split" not in name and " Input " not in name:
            return ["Mic (input 1)", "Inst (input 2)"]
        return [f"input {i + 1}" for i in range(n)]

    def _refresh_input_names(self):
        names = self._input_names()
        for hd in self.headers.values():
            hd.set_inputs(names)

    def audio_settings(self):
        if self.engine.recording or self.engine.playing:
            self.stop_all()
        host = self.settings.value("host") or eng.preferred_host()
        d = AudioSettingsDialog(self, host, self.settings.value("input"), self.settings.value("output"),
                                int(self.settings.value("blocksize", 1024)),
                                float(self.settings.value("manual_latency_ms", 0.0)),
                                self.engine.base_latency_ms())
        if d.exec():
            host, inp, out, block, manual = d.values()
            self.settings.setValue("host", host)
            self.settings.setValue("input", inp if inp else "__none__")
            self.settings.setValue("output", out)
            self.settings.setValue("blocksize", block)
            self.settings.setValue("manual_latency_ms", manual)
            self._open_audio()

    # ================================================================ projects
    def _fill_recent(self):
        self.recent.clear()
        for f in self._recent_list():
            it = QListWidgetItem(f"{os.path.basename(f)}    —    {f}")
            it.setData(Qt.UserRole, f)
            self.recent.addItem(it)
        self.recent_label.setVisible(self.recent.count() > 0)
        self.recent.setVisible(self.recent.count() > 0)

    def _recent_list(self) -> list[str]:
        r = self.settings.value("recent", []) or []
        if isinstance(r, str):
            r = [r]
        return [f for f in r if os.path.exists(os.path.join(f, "project.json"))]

    def _add_recent(self, folder):
        r = [folder] + [f for f in self._recent_list() if f != folder]
        self.settings.setValue("recent", r[:10])
        self._fill_recent()

    def new_project(self):
        start = self.settings.value("last_video_dir", os.path.expanduser("~/Videos"))
        path, _ = QFileDialog.getOpenFileName(self, "Pick a video", start, VIDEO_EXTS + ";;All files (*)")
        if not path:
            return
        self.settings.setValue("last_video_dir", os.path.dirname(path))
        try:
            info = media.probe(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Can't use that video", str(e))
            return
        stem = re.sub(r"[^\w\- ]+", "_", os.path.splitext(os.path.basename(path))[0]).strip() or "project"
        name, ok = QInputDialog.getText(self, "New project", "Project name:", text=stem)
        if not ok or not name.strip():
            return
        folder = self._new_folder(name)
        self.stop_all()
        length = int(round(info["duration"] * SR))
        dlg = QProgressDialog("Reading the video's audio...", None, 0, 0, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()

        def job(_progress):
            audio = media.extract_audio(path, length) if info["has_audio"] else None
            os.makedirs(folder)
            p = Project(folder, os.path.abspath(path), length, info)
            phone = Track(name="Phone audio", kind="video", channels=2, color=TRACK_COLORS[0], audio=audio)
            p.tracks.append(phone)
            p.tracks.append(Track(name="Bass", input_channel=1 if self.engine.in_channels >= 2 else 0,
                                  color=p.next_color()))
            p.save()
            return p

        def done(p):
            dlg.close()
            self._load(p)

        def failed(err):
            dlg.close()
            QMessageBox.warning(self, "Couldn't create project", err)

        self._run(job, done, failed)

    def _new_folder(self, name: str) -> str:
        """~/Overdub/<name>, or <name> 2, <name> 3... if taken. Not created yet."""
        name = re.sub(r"[/\\]+", "_", name.strip())
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        folder = os.path.join(PROJECTS_DIR, name)
        n = 2
        while os.path.exists(folder):
            folder = os.path.join(PROJECTS_DIR, f"{name} {n}")
            n += 1
        return folder

    def new_scratch_project(self):
        d = ScratchDialog(self, f"Song {time.strftime('%Y-%m-%d')}")
        if not d.exec() or not d.name.text().strip():
            return
        folder = self._new_folder(d.name.text())
        self.stop_all()
        try:
            os.makedirs(folder)
            p = Project(folder, "", d.length.seconds() * SR, {})
            p.tracks.append(Track(name="Bass", input_channel=1 if self.engine.in_channels >= 2 else 0,
                                  color=p.next_color()))
            p.save()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Couldn't create project", str(e))
            return
        self._load(p)

    def change_length(self):
        p = self.project
        if not p or p.has_video:
            return
        d = LengthDialog(self, round(p.length / SR))
        if not d.exec():
            return
        n = d.length.seconds() * SR
        if n == p.length:
            return
        if n < p.length and any(t.audio is not None and np.abs(t.audio[n:]).max(initial=0) > 1e-4
                                for t in p.tracks):
            if QMessageBox.question(self, "Change length",
                                    f"There's audio after {fmt_time(n)} that will be cut off. Carry on?"
                                    ) != QMessageBox.Yes:
                return
        self.stop_all()
        p.set_length(n)
        self.state.length = n
        self.state.cursor = min(self.state.cursor, n)
        self.state.sel = None
        for t in p.tracks:
            self.renderer.request(t, 0)
        self._fit_view()
        self.state.changed.emit()
        self.save()
        self.status.showMessage(f"Length is now {fmt_time(n)} (undo history cleared)", 5000)

    def open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open project", PROJECTS_DIR, "Overdub project (project.json)")
        if path:
            self.load_project_folder(os.path.dirname(path))

    def load_project_folder(self, folder):
        self.stop_all()
        try:
            p = Project.load(folder)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Couldn't open project", str(e))
            return
        if p.has_video and not os.path.exists(p.video_path):
            QMessageBox.information(self, "Video moved?",
                                    f"The original video isn't at\n{p.video_path}\nanymore. Please find it.")
            path, _ = QFileDialog.getOpenFileName(self, "Find the video", os.path.expanduser("~"), VIDEO_EXTS)
            if path:
                p.video_path = path
                p.dirty = True
        self._load(p)

    def _load(self, p: Project):
        if self.project and self.project.dirty:
            self.save()
        self.project = p
        self.engine.project = p
        self.renderer.project = p
        self.state.length = p.length
        self.state.cursor = 0
        self.state.sel = None
        self.state.playhead = None
        self.state.sel_track = None
        self.master_vol.setValue(p.master_volume_db)
        self.engine.update_master()
        self._rebuild_tracks()
        for t in p.tracks:
            self.renderer.request(t, 0)
        sel = next((t for t in p.tracks if t.kind == "audio"), p.tracks[0])
        self.select_track(sel.id)
        self.stack.setCurrentIndex(1)
        self.setWindowTitle(f"Overdub — {p.name}")
        self._add_recent(p.folder)
        QTimer.singleShot(50, self._fit_view)
        self.player.stop()
        self.player.setSource(QUrl())
        self.video_box.setVisible(p.has_video)
        self.video_msg.setText("")
        self.length_act.setEnabled(not p.has_video)
        self.export_btn.setText("Export video" if p.has_video else "Export WAV")
        self.export_btn.setToolTip("Export the video with your mix (Ctrl+E)" if p.has_video
                                   else "Export the mix as a WAV file (Ctrl+E)")
        if p.has_video and os.path.exists(p.proxy_path):
            self._set_video(p.proxy_path)
        elif p.has_video and os.path.exists(p.video_path):
            self._build_proxy(p)
        if p.dirty:
            self.save()

    def _fit_view(self):
        self.state.lane_width = max(200, self.track_area.viewport().width() - HEADER_W)
        self.state.zoom_fit()

    def _build_proxy(self, p: Project):
        self.video_msg.setText("Building video preview...")
        tmp = p.proxy_path + ".part.mp4"
        dur = p.video_info.get("duration", 1)

        def job(progress):
            media.run_with_progress(media.make_proxy_cmd(p.video_path, tmp), dur,
                                    lambda f: progress("", f))
            os.replace(tmp, p.proxy_path)
            return p.proxy_path

        def prog(_m, f):
            self.video_msg.setText(f"Building video preview... {f * 100:.0f}%")

        def done(path):
            if self.project is p:
                self._set_video(path)

        self._run(job, done, lambda e: self.video_msg.setText(f"Preview failed: {e[:200]}"), prog)

    def _set_video(self, path):
        self.video_msg.setText("")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.pause()
        self._last_video_cursor = -1

    def save(self):
        if not self.project:
            return
        try:
            self.project.save()
            self.status.showMessage("Saved", 2000)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))

    def _autosave(self):
        if self.project and self.project.dirty and not self.engine.playing:
            self.save()

    def show_folder(self):
        if self.project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.project.folder))

    def _run(self, job, done, failed, progress=None):
        w = Worker()
        self._workers.append(w)
        w.done.connect(done)
        w.failed.connect(failed)
        if progress:
            w.progress.connect(progress)
        w.done.connect(lambda *_: self._workers.remove(w) if w in self._workers else None)
        w.failed.connect(lambda *_: self._workers.remove(w) if w in self._workers else None)
        w.start(job)
        return w

    # ================================================================ tracks
    def _rebuild_tracks(self):
        while self.rows.count() > 1:
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.headers.clear()
        self.lanes.clear()
        names = self._input_names()
        for t in self.project.tracks:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(0)
            hd = TrackHeader(t, names)
            hd.selected.connect(self.select_track)
            hd.changed.connect(self._header_changed)
            lane = Lane(t, self.state, self.peaks, self._lane_engine_info)
            lane.pressed.connect(self.select_track)
            lane.context.connect(self._lane_menu)
            lane.moveRequested.connect(self._move_region)
            h.addWidget(hd)
            h.addWidget(lane, 1)
            self.rows.insertWidget(self.rows.count() - 1, row)
            self.headers[t.id] = hd
            self.lanes[t.id] = lane
        if self.state.sel_track in self.headers:
            self.headers[self.state.sel_track].set_selected(True)

    def _lane_engine_info(self):
        return self.engine, self.rec_track_id, self.live_peaks, self.engine.latency_samples()

    def selected_track(self) -> Track | None:
        return self.project.track(self.state.sel_track) if self.project and self.state.sel_track else None

    def select_track(self, tid: str):
        if not self.project or self.project.track(tid) is None:
            return
        if self.engine.recording and tid != self.rec_track_id:
            return
        old = self.state.sel_track
        self.state.sel_track = tid
        for i, hd in self.headers.items():
            hd.set_selected(i == tid)
        if old != tid or self.fx_panel.is_master or self.fx_panel.fx is None:
            t = self.project.track(tid)
            self.master_btn.setChecked(False)
            self.fx_panel.set_target(t.fx, t.name)
        self.state.changed.emit()

    def select_relative(self, d):
        ids = [t.id for t in self.project.tracks]
        if not ids:
            return
        i = ids.index(self.state.sel_track) if self.state.sel_track in ids else 0
        self.select_track(ids[max(0, min(len(ids) - 1, i + d))])

    def add_track(self, name: str | None = None, channel: int | None = None):
        if not self.project:
            return
        n = sum(1 for t in self.project.tracks if t.kind == "audio") + 1
        ch = channel if channel is not None else 0
        ch = min(ch, max(0, self.engine.in_channels - 1))
        t = Track(name=name or f"Track {n}", input_channel=ch, color=self.project.next_color())
        self.project.tracks.append(t)
        self.project.dirty = True
        self._rebuild_tracks()
        self.select_track(t.id)
        QTimer.singleShot(50, lambda: self.track_area.verticalScrollBar().setValue(10 ** 6))

    def _header_changed(self, tid, what):
        t = self.project.track(tid)
        if t is None:
            return
        self.project.dirty = True
        if what == "mix":
            self.engine.update_mix()
            self.lanes[tid].update()
        elif what == "name":
            self._rename(tid)
        elif what == "delete":
            self._delete(tid)

    def _rename(self, tid):
        t = self.project.track(tid) if self.project and tid else None
        if not t:
            return
        name, ok = QInputDialog.getText(self, "Rename track", "Name:", text=t.name)
        if ok and name.strip():
            t.name = name.strip()
            self.headers[tid].refresh()
            if not self.fx_panel.is_master and self.state.sel_track == tid:
                self.fx_panel.set_target(t.fx, t.name)
            self.project.dirty = True

    def _delete(self, tid):
        t = self.project.track(tid) if self.project and tid else None
        if not t or t.kind != "audio" or self.engine.recording:
            return
        if t.audio is not None and QMessageBox.question(
                self, "Delete track", f"Delete '{t.name}'? (You can undo this.)") != QMessageBox.Yes:
            return
        self.project.delete_track(t)
        self._after_structure_change()

    def _after_structure_change(self):
        ids = [t.id for t in self.project.tracks]
        if self.state.sel_track not in ids:
            self.state.sel_track = None
        self._rebuild_tracks()
        self.engine.update_mix()
        self.select_track(self.state.sel_track or ids[0])

    def _toggle_flag(self, flag):
        t = self.selected_track()
        if t:
            setattr(t, flag, not getattr(t, flag))
            self.headers[t.id].refresh()
            self._header_changed(t.id, "mix")

    def _on_render_done(self, tid):
        self.engine.update_mix()

    def _on_fx_changed(self):
        if not self.project:
            return
        self.project.dirty = True
        if self.fx_panel.is_master:
            self.engine.update_master()
        else:
            t = self.selected_track()
            if t:
                self.renderer.request(t)

    def show_master_fx(self):
        if not self.project:
            self.master_btn.setChecked(False)
            return
        if self.fx_panel.is_master:
            self.master_btn.setChecked(False)
            t = self.selected_track()
            self.fx_panel.set_target(t.fx if t else None, t.name if t else "")
        else:
            self.master_btn.setChecked(True)
            self.fx_panel.set_target(self.project.master_fx, "Master bus (whole mix)", is_master=True)

    def _master_vol(self, v):
        if self.project:
            self.project.master_volume_db = v
            self.project.dirty = True
            self.engine.update_mix()

    def _track_audio_changed(self, t: Track):
        self.renderer.request(t, 0)
        if t.id in self.lanes:
            self.lanes[t.id].update()

    # ================================================================ transport
    def set_cursor(self, s):
        self.state.cursor = max(0, min(self.project.length, int(s)))
        self.state.changed.emit()
        self._ensure_visible(self.state.cursor)

    def move_cursor(self, secs):
        self.state.sel = None
        self.set_cursor(self.state.cursor + secs * SR)

    def go_start(self):
        if not self.project:
            return
        if self.engine.playing:
            self.stop_all()
        self.state.sel = None
        self.set_cursor(0)

    def _ruler_click(self, s):
        if self.engine.playing and not self.engine.recording:
            self.engine.pos = s
            self.play_origin = s
            self.state.sel = None
        self.state.cursor = s
        self.state.changed.emit()

    def toggle_loop(self):
        self.state.loop = self.loop_btn.isChecked()
        if self.engine.playing and not self.engine.recording and self.state.sel:
            self.engine.loop = self.state.sel if self.state.loop else None
            self.engine.stop_at = None if self.state.loop else self.state.sel[1]
        self.status.showMessage(f"Loop {'on' if self.state.loop else 'off'}", 1500)
        self.state.changed.emit()

    def toggle_play(self):
        if not self.project:
            return
        if self.engine.recording:
            self.finish_recording()
        elif self.engine.playing:
            self.stop_play()
        else:
            self.start_play()

    def start_play(self):
        if self.engine.stream is None:
            self.status.showMessage("No audio device open. Check Audio settings.", 4000)
            return
        sel = self.state.sel
        start = sel[0] if sel else self.state.cursor
        if start >= self.project.length - SR // 20:
            start = 0
        loop = sel if (sel and self.state.loop) else None
        stop_at = sel[1] if (sel and not self.state.loop) else None
        self.play_origin = start
        self.engine.play(start, stop_at=stop_at, loop=loop)
        self._video_start(start)
        self.play_btn.setText("■")

    def stop_play(self):
        self.engine.stop()
        self.state.playhead = None
        self.state.cursor = self.play_origin
        self._video_stop()
        self.play_btn.setText("▶")
        self.state.changed.emit()

    def stop_all(self):
        if self.engine.recording:
            self.finish_recording()
        elif self.engine.playing:
            self.stop_play()

    def escape(self):
        if self.engine.recording:
            self.finish_recording(cancel=True)
        elif self.engine.playing:
            self.stop_play()
        else:
            self.state.sel = None
            self.state.changed.emit()

    def toggle_record(self):
        if not self.project:
            return
        if self.engine.recording:
            self.finish_recording()
            return
        t = self.selected_track()
        if t is None:
            return
        if t.kind != "audio":
            self.status.showMessage("That's the video's own audio. Select (or add) another track to record on.", 5000)
            return
        if self.engine.stream is None or self.engine.in_channels == 0:
            QMessageBox.information(self, "No input", "No input device is selected. Pick one in Audio settings.")
            return
        lat = self.engine.latency_samples()
        ch = min(t.input_channel, self.engine.in_channels - 1)
        self.rec_track_id = t.id
        # don't play the track we're recording over, it's distracting
        self.engine.exclude_track = t.id
        self.engine.update_mix()
        self.live_peaks = np.zeros(0, np.float32)
        self._live_idx = 0
        self._live_tail = np.zeros(0, np.float32)
        if self.engine.playing:
            # punch in on the fly at the current position
            pos = self.engine.pos
            self.punch = (pos, None)
            self.engine.rec_blocks = []
            self.engine.rec_first_pos = pos
            self.engine.rec_channel = ch
            self.engine.loop = None
            self.engine.stop_at = None
            self.engine.recording = True
        else:
            sel = self.state.sel
            if sel:
                start = sel[0]
                self.punch = (sel[0], sel[1])
                stop_at = min(self.project.length, sel[1] + lat)
            else:
                start = self.state.cursor
                if start >= self.project.length - SR // 10:
                    start = 0
                self.punch = (start, None)
                stop_at = None
            self.play_origin = start
            self.engine.record(start, ch, stop_at)
            self._video_start(start)
        self.rec_btn.setChecked(True)
        self.rec_btn.setStyleSheet("background:#c62828;color:white")
        self.play_btn.setText("■")
        self.status.showMessage(f"Recording on '{t.name}'  (R or Space to stop, Esc to cancel)")

    def finish_recording(self, cancel=False):
        res = self.engine.stop()
        tid, self.rec_track_id = self.rec_track_id, None
        self.engine.exclude_track = None
        self.engine.update_mix()
        self.rec_btn.setStyleSheet("")
        self.play_btn.setText("▶")
        self.state.playhead = None
        self._video_stop()
        self.live_peaks = np.zeros(0, np.float32)
        t = self.project.track(tid) if tid else None
        if cancel or res is None or t is None:
            self.status.showMessage("Recording cancelled" if cancel else "Nothing recorded", 3000)
            self.state.cursor = self.play_origin
            self.state.changed.emit()
            return
        data, first = res
        lat = self.engine.latency_samples()
        tl_start = first - lat
        p_in, p_out = self.punch
        w0 = max(p_in, tl_start, 0)
        w1 = min(p_out if p_out is not None else tl_start + len(data), tl_start + len(data), self.project.length)
        if w1 - w0 < SR // 20:
            self.status.showMessage("Take too short, ignored", 3000)
        else:
            seg = data[w0 - tl_start: w1 - tl_start].reshape(-1, 1).astype(np.float32)
            # 5 ms crossfade with what was there at the punch edges, so there are no clicks
            self.project.write_region(t, w0, seg, "Record", xfade=240)
            peak = float(np.abs(seg).max())
            msg = f"Recorded {fmt_time(w1 - w0)} on '{t.name}'"
            if peak >= 0.99:
                msg += "  —  WARNING: it clipped, turn the gain knob on the Scarlett down a bit"
            elif peak < 0.02:
                msg += "  —  that's very quiet, is the right input selected?"
            self.status.showMessage(msg, 8000)
            self._track_audio_changed(t)
            self.save()
        self.state.cursor = self.play_origin
        self.state.changed.emit()

    # ================================================================ editing
    def _sel_target(self):
        t = self.selected_track()
        if not t or not self.state.sel or self.engine.recording:
            return None, 0, 0
        return t, self.state.sel[0], self.state.sel[1]

    def silence_selection(self):
        t, a, b = self._sel_target()
        if t:
            self.project.transform_region(t, a, b, np.zeros_like, "Silence")
            self._track_audio_changed(t)

    def fade_selection(self, fade_in: bool):
        t, a, b = self._sel_target()
        if t:
            def fn(seg):
                ramp = np.linspace(0, 1, len(seg)) if fade_in else np.linspace(1, 0, len(seg))
                return seg * ramp[:, None] ** 2
            self.project.transform_region(t, a, b, fn, "Fade")
            self._track_audio_changed(t)

    def normalize_selection(self):
        t, a, b = self._sel_target()
        if t:
            def fn(seg):
                pk = np.abs(seg).max()
                return seg * (0.89 / pk) if pk > 1e-5 else seg
            self.project.transform_region(t, a, b, fn, "Normalise")
            self._track_audio_changed(t)

    def gain_selection(self, db: float):
        t, a, b = self._sel_target()
        if not t:
            self.status.showMessage("Select part of a track first (click and drag on it)", 3000)
            return
        g = 10 ** (db / 20)

        def fn(seg):
            env = np.full(len(seg), g, np.float32)
            k = min(240, len(seg) // 2)      # 5 ms ramps so the level change doesn't click
            env[:k] = np.linspace(1, g, k)
            env[-k:] = np.linspace(g, 1, k)
            return seg * env[:, None]
        self.project.transform_region(t, a, b, fn, f"Level {db:+.1f} dB")
        self._track_audio_changed(t)
        pk = float(np.abs(t.audio[a:b]).max()) if t.audio is not None else 0
        warn = "  (peaks over 0 dB now, the master limiter will catch it)" if pk > 1 else ""
        self.status.showMessage(f"Selection {db:+.1f} dB{warn}", 2500)

    def gain_selection_dialog(self):
        t, a, b = self._sel_target()
        if not t:
            self.status.showMessage("Select part of a track first (click and drag on it)", 3000)
            return
        db, ok = QInputDialog.getDouble(self, "Change level", "Change the selection's level by (dB):",
                                        3.0, -60.0, 24.0, 1)
        if ok and db:
            self.gain_selection(db)

    def copy(self):
        t, a, b = self._sel_target()
        if t and t.audio is not None:
            self.clipboard = t.audio[a:b].copy()
            self.status.showMessage(f"Copied {(b - a) / SR:.2f} s", 2000)

    def cut(self):
        t, a, b = self._sel_target()
        if t and t.audio is not None:
            self.clipboard = t.audio[a:b].copy()
            self.project.write_region(t, a, np.zeros_like(self.clipboard), "Cut", xfade=240)
            self._track_audio_changed(t)
            self.status.showMessage(f"Cut {(b - a) / SR:.2f} s", 2000)

    def paste(self):
        t = self.selected_track()
        clip = getattr(self, "clipboard", None)
        if t is None or clip is None or self.engine.recording:
            return
        at = self.state.sel[0] if self.state.sel else self.state.cursor
        n = min(len(clip), self.project.length - at)
        if n <= 0:
            return
        self.project.write_region(t, at, clip[:n], "Paste", xfade=240)
        self.state.sel = (at, at + n)
        self.state.cursor = at
        self._track_audio_changed(t)
        self.status.showMessage(f"Pasted {n / SR:.2f} s onto '{t.name}' (overwrote what was there)", 3000)

    def _move_region(self, tid, a, b, dst, copy):
        t = self.project.track(tid)
        if t is None or self.engine.recording:
            return
        n = self.project.move_region(t, a, b, dst, copy)
        if n:
            self.state.sel = (dst, dst + n)
            self.state.cursor = dst
            self._track_audio_changed(t)
            self.status.showMessage(f"{'Copied' if copy else 'Moved'} {n / SR:.2f} s "
                                    f"({(dst - a) / SR:+.3f} s). Ctrl+Z to undo", 3000)
        self.state.changed.emit()

    def select_all(self):
        self.state.sel = (0, self.project.length)
        self.state.cursor = 0
        self.state.changed.emit()

    def nudge(self, ms):
        t = self.selected_track()
        if t and not self.engine.recording:
            self.project.set_offset(t, round(t.offset_ms + ms, 1))
            self._track_audio_changed(t)
            self.status.showMessage(f"'{t.name}' offset {t.offset_ms:+.0f} ms", 2000)

    def undo(self):
        self._undo_redo(self.project.undo if self.project else None, "Undid")

    def redo(self):
        self._undo_redo(self.project.redo if self.project else None, "Redid")

    def _undo_redo(self, fn, word):
        if fn is None or self.engine.recording:
            return
        before = [t.id for t in self.project.tracks]
        res = fn()
        if res is None:
            self.status.showMessage("Nothing to " + ("undo" if word == "Undid" else "redo"), 2000)
            return
        label, tid = res
        if [t.id for t in self.project.tracks] != before:
            self._after_structure_change()
        t = self.project.track(tid) if tid else None
        if t:
            self._track_audio_changed(t)
        self.status.showMessage(f"{word}: {label}", 2000)

    def _lane_menu(self, tid, pos):
        m = QMenu(self)
        has_sel = self.state.sel is not None
        for text, fn in (("Louder +1 dB  (Shift+Up)", lambda: self.gain_selection(1.0)),
                         ("Quieter -1 dB  (Shift+Down)", lambda: self.gain_selection(-1.0)),
                         ("Change level...  (G)", self.gain_selection_dialog),
                         ("Cut", self.cut), ("Copy", self.copy),
                         ("Silence selection", self.silence_selection),
                         ("Fade in", lambda: self.fade_selection(True)),
                         ("Fade out", lambda: self.fade_selection(False)),
                         ("Normalise", self.normalize_selection)):
            a = m.addAction(text, fn)
            a.setEnabled(has_sel)
        m.addSeparator()
        m.addAction("Select whole track", self.select_all)
        m.addAction("Rename track...", lambda: self._rename(tid))
        t = self.project.track(tid)
        if t and t.kind == "audio":
            m.addAction("Delete track", lambda: self._delete(tid))
        m.exec(pos)

    # ================================================================ export
    def export(self):
        if not self.project:
            return
        if not self.project.has_video:
            self.export_wav()
            return
        self.stop_all()
        p = self.project
        default = self.settings.value("last_export_dir", os.path.expanduser("~/Videos"))
        d = ExportDialog(self, os.path.join(default, f"{p.name}.mp4"), self.state.sel)
        if not d.exec():
            return
        out = d.path.text().strip()
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        if os.path.abspath(out) == os.path.abspath(p.video_path):
            QMessageBox.warning(self, "Export", "Pick a different file name, that's your original video.")
            return
        self.settings.setValue("last_export_dir", os.path.dirname(out))
        part = d.part.isChecked() and self.state.sel is not None
        a, b = (self.state.sel if part else (0, p.length))
        normalize = d.normalize.isChecked()
        bitrate = d.bitrate.currentText()
        info = p.video_info

        dlg = QProgressDialog("Exporting...", None, 0, 100, self)
        dlg.setWindowTitle("Export")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        dlg.show()

        def job(progress):
            mix = render_mix(p, a, b, normalize, lambda m, f: progress(m, f * 0.4))
            wav = os.path.join(p.folder, "export_mix.wav")
            sf.write(wav, mix, SR, subtype="PCM_24")
            progress("Writing video...", 0.4)
            cmd = media.export_cmd(p.video_path, wav, out, info,
                                   a / SR if part else None, b / SR if part else None, bitrate)
            media.run_with_progress(cmd, (b - a) / SR, lambda f: progress("Writing video...", 0.4 + 0.6 * f))
            os.remove(wav)
            return out

        def prog(m, f):
            dlg.setLabelText(m)
            dlg.setValue(int(f * 100))

        def done(path):
            dlg.close()
            box = QMessageBox(self)
            box.setWindowTitle("Exported")
            box.setText(f"Saved:\n{path}")
            openb = box.addButton("Open video", QMessageBox.ActionRole)
            folderb = box.addButton("Show folder", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is openb:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            elif box.clickedButton() is folderb:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

        def failed(err):
            dlg.close()
            QMessageBox.warning(self, "Export failed", err[-1500:])

        self._run(job, done, failed, prog)

    def export_wav(self):
        if not self.project:
            return
        self.stop_all()
        p = self.project
        path, _ = QFileDialog.getSaveFileName(self, "Export mix", os.path.join(os.path.expanduser("~/Music"),
                                              f"{p.name}.wav"), "WAV (*.wav)")
        if not path:
            return
        def job(_):
            sf.write(path, render_mix(p, 0, p.length, True), SR, subtype="PCM_24")
            return path
        self._run(job, lambda pth: self.status.showMessage(f"Saved {pth}", 5000),
                  lambda e: QMessageBox.warning(self, "Export failed", e))

    # ================================================================ video sync
    def _video_target_ms(self, pos):
        out_lat = 0.0
        if self.engine.stream is not None:
            lat = self.engine.stream.latency
            out_lat = lat[1] if isinstance(lat, (tuple, list)) else lat
        return max(0, int(pos / SR * 1000 - out_lat * 1000))

    def _video_start(self, pos):
        if self.player.source().isEmpty():
            return
        self.player.setPosition(self._video_target_ms(pos))
        self.player.play()
        self._last_video_seek = time.monotonic()

    def _video_stop(self):
        if self.player.source().isEmpty():
            return
        self.player.pause()
        self._last_video_cursor = -1

    def _sync_video(self):
        if self.player.source().isEmpty():
            return
        if self.engine.playing:
            target = self._video_target_ms(self.engine.pos)
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.player.play()
            now = time.monotonic()
            if abs(self.player.position() - target) > 150 and now - self._last_video_seek > 0.6:
                self.player.setPosition(target)
                self._last_video_seek = now
        else:
            c = self.state.sel[0] if self.state.sel else self.state.cursor
            if c != self._last_video_cursor:
                self._last_video_cursor = c
                self.player.setPosition(int(c / SR * 1000))

    # ================================================================ timer
    def _tick(self):
        e = self.engine
        if self.project is None:
            return
        if e.ended and e.playing:
            if e.recording:
                self.finish_recording()
            else:
                self.stop_play()
        if e.playing:
            self.state.playhead = e.pos
            self._ensure_visible(e.pos, follow=True)
        if e.recording:
            self._update_live_peaks()
        shown = e.pos if e.playing else self.state.cursor
        self.time_label.setText(fmt_time(shown))
        if self.state.sel:
            a, b = self.state.sel
            self.sel_label.setText(f"sel {fmt_time(a)} – {fmt_time(b)}  ({(b - a) / SR:.2f} s)")
        else:
            self.sel_label.setText("")
        t = self.selected_track()
        if t is not None and t.kind == "audio" and e.in_channels:
            self.in_meter.set_peak(float(e.in_peaks[min(t.input_channel, e.in_channels - 1)]))
        else:
            self.in_meter.set_peak(0.0)
        self.out_meter.set_peak(e.out_peak)
        self._sync_video()
        if e.playing:
            self.state.changed.emit()

    def _update_live_peaks(self):
        blocks = self.engine.rec_blocks
        n = len(blocks)
        if n <= self._live_idx:
            return
        new = np.concatenate([self._live_tail] + blocks[self._live_idx:n])
        self._live_idx = n
        k = len(new) // PEAK_BIN
        if k:
            pk = np.abs(new[: k * PEAK_BIN]).reshape(k, PEAK_BIN).max(axis=1)
            self.live_peaks = np.concatenate([self.live_peaks, pk.astype(np.float32)])
        self._live_tail = new[k * PEAK_BIN:]

    def _ensure_visible(self, s, follow=False):
        st = self.state
        x = st.s_to_x(s)
        if x < 0 or x > st.lane_width * (0.92 if follow else 1.0):
            st.set_offset(s / SR - (st.visible_seconds() * (0.08 if follow else 0.3)))

    def _sync_scrollbar(self):
        st = self.state
        self.hscroll.blockSignals(True)
        self.hscroll.setRange(0, int(max(0.0, st.length / SR - st.visible_seconds() * 0.9) * 1000))
        self.hscroll.setPageStep(int(st.visible_seconds() * 1000))
        self.hscroll.setValue(int(st.offset * 1000))
        self.hscroll.blockSignals(False)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._update_lane_width)

    def _update_lane_width(self):
        w = max(200, self.track_area.viewport().width() - HEADER_W)
        if w != self.state.lane_width:
            self.state.lane_width = w
            self._sync_scrollbar()
            self.state.changed.emit()

    def closeEvent(self, e):
        self.stop_all()
        if self.project and self.project.dirty:
            self.save()
        self.engine.close()
        super().closeEvent(e)
