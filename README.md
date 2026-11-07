# Overdub

Small Linux multitrack recorder built around a video. Pick a video you shot on your phone, record bass/vocals/whatever over it through your audio interface, mix it with per-track effects, then export the original video with the new mix.

Run it with `./run.sh` or from the app menu ("Overdub").

## How it works

- **New project from video**: the video's own audio becomes the "Phone audio" track and an empty "Bass" track gets added (set to Inst / input 2). Projects live in `~/Overdub/<name>/`.
- **Recording**: select a track and press **R**. It overwrites whatever is on that track from the cursor onward. If you select a range first, R punches in: 2 s of pre-roll, and it only records inside the selection.
- **Hearing yourself**: turn on Direct Monitor on the Scarlett. The app plays the backing tracks and shifts each new take back by the round-trip latency so it lines up.
- **Effects per track**: noise gate, EQ, compressor, reverb (small room / room / hall / plate / cathedral), plus presets. The master bus has its own compressor, reverb and limiter.
- **Export**: whole video = the picture is copied untouched (full quality, no re-encode) and the mix goes in as 320k AAC. Exporting just a selection re-encodes the video.

Press **F1** in the app for all the shortcuts. The main ones: Space play/stop, R record, Esc cancel take, L loop, M/S mute/solo, Delete silence selection, Alt+arrows nudge a track by 5 ms, Ctrl+Z undo.

## If takes land late or early

Audio > Audio settings > "Extra latency fix". Record yourself clapping along to a clap in the video, zoom in (Ctrl+wheel), and see how far off it is. Positive = takes were late.

## Bits

- Python + PySide6 (Qt), `sounddevice` (PortAudio via PipeWire's JACK layer), Spotify's `pedalboard` for effects, ffmpeg for decoding/export.
- `PIPEWIRE_RATE=1/48000` is set at startup so the stream opens at 48k regardless of what else is playing.
- Effects are rendered in a background thread whenever you change a setting, so playback just mixes pre-rendered buffers. Volume/pan/mute/solo are live.
- `dev/screenshot.py` renders the UI offscreen for checking layout.
