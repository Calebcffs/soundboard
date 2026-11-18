# Overdub

Small Linux multitrack recorder built around a video. Pick a video you shot on your phone, record bass/vocals/whatever over it through your audio interface, mix it with per-track effects, then export the original video with the new mix.

Run it with `./run.sh` or from the app menu ("Overdub").

## How it works

- **New project from video**: the video's own audio becomes the "Phone audio" track and an empty "Bass" track gets added (set to Inst / input 2). Projects live in `~/Overdub/<name>/`.
- **Start from scratch**: no video, just an empty "Bass" track and a timeline length you pick (3:00 by default). Edit > Change length grows or trims it later. Export gives you a WAV of the mix.
- **Recording**: select a track and press **R**. It overwrites whatever is on that track from the cursor onward, and that track is muted while you record so you don't hear the old take. If you select a range first, R records exactly inside the selection (5 ms crossfades at the edges so there's no click).
- **Editing a selection**: drag it to move it along the track (Ctrl-drag copies), Shift+Up/Down for +/-1 dB, G for any level change, Ctrl+X/C/V to cut/copy/paste (works across tracks), Delete to silence.
- **Hearing yourself**: turn on Direct Monitor on the Scarlett. The app plays the backing tracks and shifts each new take back by the round-trip latency so it lines up.
- **Effects per track**: noise gate, EQ, compressor, reverb (small room / room / hall / plate / cathedral), plus presets. The master bus has its own compressor, reverb and limiter.
- **Export**: whole video = the picture is copied untouched (full quality, no re-encode) and the mix goes in as 320k AAC. Exporting just a selection re-encodes the video.

Press **F1** in the app for all the shortcuts. The main ones: Space play/stop, R record, Esc cancel take, L loop, M/S mute/solo, Delete silence selection, Alt+arrows nudge a track by 5 ms, Ctrl+Z undo.

## Latency (do this once)

**Audio > Measure latency.** It plays 8 clicks and listens for them (hold a headphone cup against the mic, or cable the line out into input 2), then shifts every take by exactly that much. It's remembered per device and buffer size.

Until you measure, it estimates from the driver. On PipeWire the driver figure is missing one processing cycle (checked with a loopback: real = reported + 21.3 ms at every buffer size), so that's added, but the reported part still wobbles by about 10 ms between runs. Measuring is the fix.

If takes still feel off, Audio settings > "Extra latency fix" is added on top (positive = takes were late).

## Bits

- Python + PySide6 (Qt), `sounddevice` (PortAudio via PipeWire's JACK layer), Spotify's `pedalboard` for effects, ffmpeg for decoding/export.
- `PIPEWIRE_RATE=1/48000` is set at startup so the stream opens at 48k regardless of what else is playing.
- Effects are rendered in a background thread whenever you change a setting, so playback just mixes pre-rendered buffers. Volume/pan/mute/solo are live.
- `dev/screenshot.py` renders the UI offscreen for checking layout.
