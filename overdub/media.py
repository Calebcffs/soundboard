"""ffmpeg/ffprobe helpers: probing, audio extraction, preview proxy, final export."""
from __future__ import annotations

import json
import subprocess

import numpy as np

from .project import SR


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout
    doc = json.loads(out)
    v = next((s for s in doc["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in doc["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValueError("That file has no video stream.")
    dur = float(v.get("duration") or doc["format"].get("duration") or 0)
    if dur <= 0:
        raise ValueError("Couldn't work out the video's duration.")
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"duration": dur, "width": w, "height": h, "vcodec": v.get("codec_name", ""),
            "has_audio": a is not None, "rotation": rot}


def extract_audio(path: str, length: int) -> np.ndarray:
    """Decode the video's audio to (length, 2) float32 at SR."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True, check=True)
    a = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
    if len(a) < length:
        a = np.pad(a, ((0, length - len(a)), (0, 0)))
    return np.ascontiguousarray(a[:length])


def make_proxy_cmd(src: str, dst: str) -> list[str]:
    # small, keyframe-dense H.264 so the preview seeks instantly
    scale = "scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)'"
    return ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", "-i", src, "-an",
            "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "12",
            "-pix_fmt", "yuv420p", dst]


def export_cmd(video: str, wav: str, out: str, info: dict, start_s: float | None, end_s: float | None,
               bitrate: str = "320k") -> list[str]:
    cmd = ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
    if start_s is None:
        # whole video: copy the picture untouched, full original quality
        cmd += ["-i", video, "-i", wav, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
        if info.get("vcodec") == "hevc":
            cmd += ["-tag:v", "hvc1"]   # so it plays on Apple devices too
    else:
        # trimmed: cutting at arbitrary frames needs a re-encode (high quality)
        cmd += ["-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", video, "-i", wav,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-crf", "17", "-preset", "medium",
                "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", bitrate, "-shortest", "-movflags", "+faststart", out]
    return cmd


def run_with_progress(cmd: list[str], total_s: float, on_progress) -> None:
    """Run ffmpeg with -progress pipe:1, calling on_progress(fraction)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=")[1])
                on_progress(min(1.0, us / 1e6 / max(total_s, 0.001)))
            except ValueError:
                pass
    err = p.stderr.read()
    if p.wait() != 0:
        raise RuntimeError(err.strip() or "ffmpeg failed")
