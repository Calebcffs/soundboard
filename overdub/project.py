"""Project model: tracks, effects settings, undo, save/load.

Track audio arrays are treated as immutable: every edit makes a new array.
That way the audio thread, the effects renderer and the saver can all hold a
reference to an old array safely while the UI swaps in a new one.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict

import numpy as np
import soundfile as sf

SR = 48000
PROJECT_VERSION = 1
MAX_UNDO = 40
XF = 240        # 5 ms crossfade at edit edges


def crossfade_edges(old: np.ndarray, new: np.ndarray, k: int) -> np.ndarray:
    """new, but its first/last k samples fade from/to old (equal-gain), avoiding clicks."""
    n = len(new)
    k = min(k, n // 2)
    if k < 2 or len(old) != n:
        return new
    out = new.copy()
    r = np.linspace(0, 1, k, dtype=np.float32)[:, None]
    out[:k] = old[:k] * (1 - r) + new[:k] * r
    out[-k:] = new[-k:] * (1 - r) + old[-k:] * r
    return out

TRACK_COLORS = ["#f2a541", "#3fc1c9", "#a78bfa", "#f472b6", "#84cc16", "#60a5fa", "#fb7185"]


@dataclass
class FxSettings:
    gate_on: bool = False
    gate_threshold: float = -50.0

    eq_on: bool = False
    low_cut: float = 20.0          # Hz, 20 = off
    low_gain: float = 0.0          # dB shelf @150Hz
    mid_gain: float = 0.0          # dB peak
    mid_freq: float = 1000.0
    high_gain: float = 0.0         # dB shelf @5kHz

    comp_on: bool = False
    comp_threshold: float = -18.0
    comp_ratio: float = 3.0
    comp_makeup: float = 0.0

    reverb_on: bool = False
    reverb_type: str = "Room"
    reverb_mix: float = 0.25

    # master-only
    limiter_on: bool = True
    limiter_threshold: float = -1.0

    @classmethod
    def from_dict(cls, d: dict) -> "FxSettings":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Track:
    name: str
    kind: str = "audio"            # "video" (the phone audio) or "audio"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    audio: np.ndarray | None = None  # (n, ch) float32, None until something is recorded
    channels: int = 1
    input_channel: int = 0
    volume_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    offset_ms: float = 0.0
    color: str = TRACK_COLORS[1]
    fx: FxSettings = field(default_factory=lambda: FxSettings(limiter_on=False))

    # runtime only
    processed: np.ndarray | None = None
    audio_dirty: bool = True

    def meta(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "channels": self.channels,
            "input_channel": self.input_channel, "volume_db": self.volume_db, "pan": self.pan,
            "mute": self.mute, "solo": self.solo, "offset_ms": self.offset_ms,
            "color": self.color, "fx": asdict(self.fx),
            "file": f"tracks/{self.id}.wav" if self.audio is not None else None,
        }


class Project:
    def __init__(self, folder: str, video_path: str, length: int, video_info: dict):
        self.folder = folder
        self.video_path = video_path
        self.video_info = video_info
        self.length = length                    # timeline length in samples (= video duration)
        self.tracks: list[Track] = []
        self.master_fx = FxSettings(limiter_on=True)
        self.master_volume_db = 0.0
        self.undo_stack: list[tuple] = []
        self.redo_stack: list[tuple] = []
        self.dirty = False

    # ---------- paths ----------
    @property
    def name(self) -> str:
        return os.path.basename(self.folder.rstrip("/"))

    @property
    def proxy_path(self) -> str:
        return os.path.join(self.folder, "preview.mp4")

    def track(self, tid: str) -> Track | None:
        return next((t for t in self.tracks if t.id == tid), None)

    def next_color(self) -> str:
        used = {t.color for t in self.tracks}
        for c in TRACK_COLORS[1:]:
            if c not in used:
                return c
        return TRACK_COLORS[1 + len(self.tracks) % (len(TRACK_COLORS) - 1)]

    # ---------- editing (all undoable) ----------
    def _ensure_audio(self, t: Track) -> np.ndarray:
        if t.audio is None:
            return np.zeros((self.length, t.channels), np.float32)
        return t.audio

    def write_region(self, t: Track, start: int, data: np.ndarray, label: str, xfade: int = 0):
        """Overwrite t.audio[start:start+len(data)] with data (copy-on-write, undoable).
        xfade > 0 blends the first/last xfade samples with what was there, so edges don't click."""
        start = max(0, start)
        end = min(self.length, start + len(data))
        if end <= start:
            return
        data = data[: end - start]
        base = self._ensure_audio(t)
        if data.shape[1] != base.shape[1]:
            data = np.repeat(data[:, :1], base.shape[1], axis=1) if data.shape[1] == 1 else data.mean(axis=1, keepdims=True)
        had_audio = t.audio is not None
        old = base[start:end].copy()
        if xfade:
            data = crossfade_edges(old, data.astype(np.float32), xfade)
        new = base.copy()
        new[start:end] = data
        t.audio = new
        t.audio_dirty = True
        self._push(("region", t.id, start, old, data.astype(np.float32).copy(), label, had_audio))

    def transform_region(self, t: Track, start: int, end: int, fn, label: str):
        if t.audio is None:
            return
        start, end = max(0, start), min(self.length, end)
        if end - start < 2:
            return
        seg = t.audio[start:end].copy()
        self.write_region(t, start, fn(seg).astype(np.float32), label)

    def move_region(self, t: Track, a: int, b: int, dst: int, copy: bool) -> int:
        """Move (or copy) [a, b) to start at dst, overwriting what's there. One undo step.
        Returns the length actually placed."""
        if t.audio is None:
            return 0
        n = min(b - a, self.length - dst)
        if n <= 0:
            return 0
        seg = t.audio[a:a + n].copy()
        lo, hi = min(a, dst), max(b, dst + n)
        region = t.audio[lo:hi].copy()
        if not copy:
            region[a - lo:b - lo] = crossfade_edges(region[a - lo:b - lo], np.zeros_like(region[a - lo:b - lo]), XF)
        i = dst - lo
        region[i:i + n] = crossfade_edges(region[i:i + n], seg, XF)
        self.write_region(t, lo, region, "Copy" if copy else "Move")
        return n

    def set_offset(self, t: Track, ms: float):
        self._push(("offset", t.id, t.offset_ms, ms, "Nudge track", True))
        t.offset_ms = ms

    def delete_track(self, t: Track):
        idx = self.tracks.index(t)
        self.tracks.remove(t)
        self._push(("delete", t, idx, "Delete track"))

    def _push(self, entry):
        self.undo_stack.append(entry)
        del self.undo_stack[:-MAX_UNDO]
        self.redo_stack.clear()
        self.dirty = True

    def undo(self) -> tuple[str, str | None] | None:
        return self._apply(self.undo_stack, self.redo_stack, undo=True)

    def redo(self) -> tuple[str, str | None] | None:
        return self._apply(self.redo_stack, self.undo_stack, undo=False)

    def _apply(self, src, dst, undo: bool):
        """Returns (label, affected_track_id) or None."""
        if not src:
            return None
        e = src.pop()
        dst.append(e)
        self.dirty = True
        kind = e[0]
        if kind == "region":
            _, tid, start, old, new, label, had_audio = e
            t = self.track(tid)
            if t is None:
                return label, None
            if undo and not had_audio:
                t.audio = None
            else:
                a = self._ensure_audio(t).copy()
                a[start:start + len(old)] = old if undo else new
                t.audio = a
            t.audio_dirty = True
            return label, tid
        if kind == "offset":
            _, tid, old, new, label, _ = e
            t = self.track(tid)
            if t:
                t.offset_ms = old if undo else new
            return label, tid
        if kind == "delete":
            _, t, idx, label = e
            if undo:
                self.tracks.insert(min(idx, len(self.tracks)), t)
            else:
                self.tracks.remove(t)
            return label, t.id
        return None

    # ---------- persistence ----------
    def save(self):
        os.makedirs(os.path.join(self.folder, "tracks"), exist_ok=True)
        for t in self.tracks:
            path = os.path.join(self.folder, "tracks", f"{t.id}.wav")
            if t.audio is not None and (t.audio_dirty or not os.path.exists(path)):
                tmp = path + ".tmp.wav"
                sf.write(tmp, t.audio, SR, subtype="FLOAT")
                os.replace(tmp, path)
                t.audio_dirty = False
        # drop wav files of deleted tracks (but keep any still referenced by undo)
        keep = {f"{t.id}.wav" for t in self.tracks}
        keep |= {f"{e[1].id}.wav" for e in self.undo_stack if e[0] == "delete"}
        for f in os.listdir(os.path.join(self.folder, "tracks")):
            if f.endswith(".wav") and f not in keep:
                os.remove(os.path.join(self.folder, "tracks", f))
        doc = {
            "version": PROJECT_VERSION,
            "sample_rate": SR,
            "video_path": self.video_path,
            "video_info": self.video_info,
            "length": self.length,
            "master_fx": asdict(self.master_fx),
            "master_volume_db": self.master_volume_db,
            "tracks": [t.meta() for t in self.tracks],
        }
        tmp = os.path.join(self.folder, "project.json.tmp")
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, os.path.join(self.folder, "project.json"))
        self.dirty = False

    @classmethod
    def load(cls, folder: str) -> "Project":
        with open(os.path.join(folder, "project.json")) as f:
            doc = json.load(f)
        p = cls(folder, doc["video_path"], int(doc["length"]), doc.get("video_info", {}))
        p.master_fx = FxSettings.from_dict(doc.get("master_fx", {}))
        p.master_volume_db = doc.get("master_volume_db", 0.0)
        for m in doc["tracks"]:
            t = Track(name=m["name"], kind=m["kind"], id=m["id"], channels=m.get("channels", 1),
                      input_channel=m.get("input_channel", 0), volume_db=m.get("volume_db", 0.0),
                      pan=m.get("pan", 0.0), mute=m.get("mute", False), solo=m.get("solo", False),
                      offset_ms=m.get("offset_ms", 0.0), color=m.get("color", TRACK_COLORS[1]),
                      fx=FxSettings.from_dict(m.get("fx", {})))
            if m.get("file"):
                data, _sr = sf.read(os.path.join(folder, m["file"]), dtype="float32", always_2d=True)
                if len(data) < p.length:
                    data = np.pad(data, ((0, p.length - len(data)), (0, 0)))
                t.audio = np.ascontiguousarray(data[: p.length])
                t.channels = t.audio.shape[1]
            t.audio_dirty = False
            p.tracks.append(t)
        return p
