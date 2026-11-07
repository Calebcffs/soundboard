"""Realtime audio: device listing, playback mixing, recording, background FX rendering."""
from __future__ import annotations

import os
import threading
import time

# PipeWire's JACK layer otherwise opens at whatever rate the graph happens to be running
# (often 44.1k if something else is playing). Ask for 48k, like `pw-jack -s 48000` does.
os.environ.setdefault("PIPEWIRE_RATE", "1/48000")
os.environ.setdefault("PIPEWIRE_QUANTUM", "1024/48000")

import numpy as np
import sounddevice as sd

from .project import SR, Project, Track
from . import fx as fxmod


# ---------------------------------------------------------------- devices
def host_apis() -> list[str]:
    return [h["name"] for h in sd.query_hostapis()]


def preferred_host() -> str:
    names = host_apis()
    for n in ("JACK Audio Connection Kit", "ALSA"):
        if n in names:
            return n
    return names[0]


def list_devices(host: str, kind: str) -> list[tuple[int, str, int]]:
    """[(index, name, channels)] for kind 'input' or 'output' on a host API."""
    try:
        hi = host_apis().index(host)
    except ValueError:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == hi and d[key] > 0:
            out.append((i, d["name"], int(d[key])))
    return out


def device_index_by_name(host: str, kind: str, name: str | None) -> int | None:
    if not name:
        return None
    for i, n, _ in list_devices(host, kind):
        if n == name:
            return i
    return None


def guess_devices(host: str) -> tuple[str | None, str | None]:
    """Pick sensible defaults: an audio interface if there is one, else the first device."""
    ins, outs = list_devices(host, "input"), list_devices(host, "output")
    def pick(devs, want, avoid=("Split", "Monitor", "Loopback", "GNOME")):
        good = [d for d in devs if not any(a in d[1] for a in avoid)]
        # PipeWire splits interfaces into mono "Input 1 Mic"/"Input 2 Inst" sources plus one raw
        # device carrying every channel; the raw one lets each track pick its own input.
        good.sort(key=lambda d: " Input " in d[1])
        for w in want:
            for d in good:
                if w.lower() in d[1].lower():
                    return d[1]
        return (good or devs or [(None, None, 0)])[0][1]
    return (pick(ins, ["Scarlett", "Focusrite", "USB"]),
            pick(outs, ["Scarlett Solo (3rd Gen.) Headphones", "Scarlett", "Focusrite", "Headphones", "USB"]))


# ---------------------------------------------------------------- engine
class AudioEngine:
    def __init__(self):
        self.project: Project | None = None
        self.stream: sd.Stream | None = None
        self.in_channels = 0
        self.out_channels = 2
        self.manual_latency_ms = 0.0
        self.error: str | None = None

        self.playing = False
        self.pos = 0                       # timeline position (samples) of next output frame
        self.stop_at: int | None = None
        self.loop: tuple[int, int] | None = None
        self.ended = False                 # set by callback when playback hit stop_at / end

        self.recording = False
        self.rec_channel = 0
        self.rec_blocks: list[np.ndarray] = []
        self.rec_first_pos = 0

        self.in_peaks = np.zeros(8, np.float32)
        self.out_peak = 0.0
        self.xruns = 0

        self._mix: list[tuple[np.ndarray, float, float]] = []
        self._master = None
        self._master_gain = 1.0
        self._reset_master = False

    # ---- stream ----
    def open(self, in_dev: int | None, out_dev: int | None, blocksize: int = 1024):
        self.close()
        self.error = None
        devs = sd.query_devices()
        try:
            if out_dev is None:
                raise RuntimeError("No output device selected.")
            self.out_channels = min(2, devs[out_dev]["max_output_channels"])
            if in_dev is not None:
                self.in_channels = min(8, devs[in_dev]["max_input_channels"])
                self.stream = sd.Stream(device=(in_dev, out_dev), samplerate=SR, blocksize=blocksize,
                                        channels=(self.in_channels, self.out_channels), dtype="float32",
                                        callback=self._duplex_cb)
            else:
                self.in_channels = 0
                self.stream = sd.OutputStream(device=out_dev, samplerate=SR, blocksize=blocksize,
                                              channels=self.out_channels, dtype="float32",
                                              callback=self._out_cb)
            self.stream.start()
        except Exception as e:  # noqa: BLE001
            self.stream = None
            self.error = str(e)
            raise

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None

    def reported_latency_ms(self) -> float:
        if self.stream is None:
            return 0.0
        lat = self.stream.latency
        if isinstance(lat, (tuple, list)):
            return (lat[0] + lat[1]) * 1000
        return lat * 2 * 1000

    def latency_samples(self) -> int:
        return int(round((self.reported_latency_ms() + self.manual_latency_ms) * SR / 1000))

    # ---- mix state (called from UI thread; callback reads the swapped references) ----
    def update_mix(self):
        p = self.project
        if p is None:
            self._mix = []
            return
        mix = []
        for t in fxmod.audible(p):
            if t.processed is not None:
                gl, gr = fxmod.track_gains(t)
                mix.append((t.processed, gl, gr))
        self._mix = mix
        self._master_gain = 10 ** (p.master_volume_db / 20)

    def update_master(self):
        if self.project is not None:
            self._master = fxmod.build_master_chain(self.project.master_fx)
            self._reset_master = True

    # ---- transport ----
    def play(self, start: int, stop_at: int | None = None, loop: tuple[int, int] | None = None):
        self.pos = max(0, min(start, self.project.length if self.project else 0))
        self.stop_at = stop_at
        self.loop = loop
        self.ended = False
        self._reset_master = True
        self.playing = True

    def record(self, start: int, channel: int, stop_at: int | None):
        self.rec_blocks = []
        self.rec_first_pos = start
        self.rec_channel = channel
        self.recording = True
        self.play(start, stop_at=stop_at)

    def stop(self) -> tuple[np.ndarray, int] | None:
        """Stop; if recording, returns (mono recorded data, timeline pos of its first sample)."""
        self.playing = False
        time.sleep(0.005)   # let an in-flight callback finish appending
        if not self.recording:
            return None
        self.recording = False
        blocks, self.rec_blocks = self.rec_blocks, []
        if not blocks:
            return None
        return np.concatenate(blocks), self.rec_first_pos

    # ---- callbacks ----
    def _out_cb(self, outdata, frames, t, status):
        self._duplex_cb(None, outdata, frames, t, status)

    def _duplex_cb(self, indata, outdata, frames, t, status):
        if status.output_underflow or status.input_overflow:
            self.xruns += 1
        if indata is not None and len(indata):
            self.in_peaks[: indata.shape[1]] = np.abs(indata).max(axis=0)
        outdata.fill(0)
        if not self.playing or self.project is None:
            self.out_peak = 0.0
            return
        length = self.project.length
        mix = np.zeros((frames, 2), np.float32)
        pos, filled = self.pos, 0
        rec = self.recording
        while filled < frames:
            seg_end = length
            if self.loop and not rec:
                seg_end = min(seg_end, self.loop[1])
            if self.stop_at is not None:
                seg_end = min(seg_end, self.stop_at)
            n = min(frames - filled, seg_end - pos)
            if n <= 0:
                if self.loop and not rec and pos >= self.loop[1] and self.loop[1] > self.loop[0]:
                    pos = self.loop[0]
                    continue
                self.ended = True
                break
            for buf, gl, gr in self._mix:
                m = min(n, len(buf) - pos)
                if m > 0:
                    mix[filled:filled + m, 0] += buf[pos:pos + m, 0] * gl
                    mix[filled:filled + m, 1] += buf[pos:pos + m, 1] * gr
            if rec and indata is not None:
                ch = min(self.rec_channel, indata.shape[1] - 1)
                self.rec_blocks.append(indata[filled:filled + n, ch].copy())
            filled += n
            pos += n
        self.pos = pos
        master = self._master
        if master is not None and len(master):
            if self._reset_master:
                master.reset()
                self._reset_master = False
            mix = master(np.ascontiguousarray(mix.T * self._master_gain), SR, reset=False).T
        else:
            mix *= self._master_gain
        if self.out_channels == 1:
            outdata[:, 0] = mix.mean(axis=1)
        else:
            outdata[:, :2] = mix
        self.out_peak = float(np.abs(mix).max())


# ---------------------------------------------------------------- background FX renderer
class Renderer:
    """Re-renders tracks' effects in a background thread, coalescing rapid slider changes."""

    def __init__(self, on_done):
        self.on_done = on_done            # called (from worker thread) with track id
        self.project: Project | None = None
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, t: Track, delay: float = 0.15):
        with self._lock:
            self._pending[t.id] = time.monotonic() + delay
        self._event.set()

    def render_now(self, t: Track):
        if self.project is not None:
            t.processed = fxmod.render_track(t, self.project.length)

    def _run(self):
        while True:
            self._event.wait(0.1)
            self._event.clear()
            now = time.monotonic()
            with self._lock:
                due = [tid for tid, at in self._pending.items() if at <= now]
                for tid in due:
                    del self._pending[tid]
            p = self.project
            for tid in due:
                t = p.track(tid) if p else None
                if t is None:
                    continue
                try:
                    t.processed = fxmod.render_track(t, p.length)
                except Exception as e:  # noqa: BLE001
                    print("render failed:", e)
                    continue
                try:
                    self.on_done(tid)
                except (RuntimeError, TypeError):
                    return      # window already closed
