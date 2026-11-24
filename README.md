# meet-recorder

A Pocket-style meeting recorder for the Linux desktop (PipeWire + Hyprland,
Omarchy/Arch). It records **both sides of a meeting** — the system/meeting audio
(via the default sink's monitor) and your microphone — mixes them, and
transcribes locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

**It is fully local. It never makes a network call and never uploads anything.**

## Requirements

- Python 3 (runtime uses the standard library only)
- [`pw-record`](https://docs.pipewire.org/) (PipeWire)
- `pactl` (PulseAudio/PipeWire control)
- `ffmpeg`
- `notify-send` (optional — notifications are skipped silently if absent)
- `faster-whisper`, installed in a venv (imported lazily, only when transcribing):

  ```sh
  python3 -m venv ~/.venvs/meet-recorder
  ~/.venvs/meet-recorder/bin/pip install faster-whisper
  ```

  The first transcription downloads the chosen Whisper model (`base` by
  default) into the local HuggingFace cache. No model pinning file is shipped;
  pin `faster-whisper` yourself in your venv if you want reproducible installs.

## Install

```sh
make install                 # copies to ~/.local/bin/meet-recorder
make install PREFIX=/custom  # -> /custom/bin/meet-recorder
```

Make sure `~/.local/bin` is on your `PATH`.

## Usage

```sh
meet-recorder start     # start recording (refuses if already recording)
meet-recorder stop      # stop, mix, print output folder; transcribes in the background
meet-recorder toggle    # start if idle, stop if recording
meet-recorder status    # "recording <elapsed-seconds>" or "idle"
meet-recorder prune     # plan what >=45-day-old meetings would be pruned
meet-recorder prune --apply [--days N]
                        # tar the text artifacts of old meetings into
                        # ~/Recordings/.archive/, delete the rest
```

`start` resolves the default sink and source, spawns two mono 16 kHz
`pw-record` processes, and writes a session state file to
`~/.local/state/meet-recorder/session.json`. Raw WAVs live in
`~/.cache/meet-recorder/<unix-ts>/`.

`stop` terminates the recorders (SIGINT, then SIGKILL fallback), mixes the two
tracks into `meeting.m4a` with ffmpeg, deletes the raw WAVs, transcribes, and
writes the results to:

```
~/Recordings/YYYY-MM-DD/HHMMSS-meeting/
├── meeting.m4a       # mixed audio
├── transcript.txt    # one utterance per line: "[mm:ss -> mm:ss] text"
└── transcript.md     # ISO start timestamp + duration heading, then the lines
```

The output folder path is printed as the last line of `stop`'s stdout.

### Hyprland keybind

Bind a single key to `toggle` so one press starts and the next stops. If you
installed faster-whisper into a venv, point the keybind at that venv's Python
so the lazy import resolves. The simplest approach is a tiny wrapper, or run the
CLI with the venv on PATH:

```ini
# ~/.config/hypr/hyprland.conf
bind = SUPER SHIFT, R, exec, ~/.venvs/meet-recorder/bin/python ~/.local/bin/meet-recorder toggle
```

(Any method that makes `faster_whisper` importable at `stop` time works — a
wrapper script that activates the venv then `exec`s the CLI is equally fine.)

## Configuration

On first run `~/.config/meet-recorder/config.ini` is created with these
defaults:

```ini
[audio]
# gain applied to system/meeting track
sys_volume = 1.0
# gain applied to mic track
mic_volume = 2.0
# capture + mix sample rate (Hz)
sample_rate = 16000
# raw | auto | <PipeWire node.name> (see below)
mic_source = raw

[whisper]
# faster-whisper model size
model = small
device = cpu
compute_type = int8
beam_size = 5
# vocabulary/context line: names, product terms
initial_prompt =
condition_on_previous_text = true

[paths]
output_dir = ~/Recordings
```

Edit it to taste; a partial file is merged over the defaults, so you only need
to list the keys you want to change. `~` in paths is expanded. Comments must
be on their own line: an inline `# ...` would become part of the value.

### Echo-free mic track

With speakers, the mic also hears the far side. Echo cancellation is
**opt-in** (`mic_source = auto`) until it has passed a real call; the default
`raw` records the mic as before. With `auto`, each recording first starts the
speaker and raw mic recorders exactly as before, then (within 3 s, never
delaying them) its own small PipeWire client process
(`pipewire -c ~/.cache/meet-recorder/<id>/aec.conf`) running WebRTC echo
cancellation on the mic the call uses, with the default speaker's monitor
as the echo reference. The PipeWire daemon never loads it, and nothing in
`~/.config/pipewire` changes. Its output is private to the recorder: the
desktop never offers it as a mic, it never becomes the default, and Teams
cannot see it. The process exits when the recording stops.

The echo-cancelled track is an extra; the raw mic is always the primary. It
is used only if it is complete (length matches), it kept your voice (in at
least 80 % of the 10 s windows where the raw mic heard you speak while the
far side was quiet), and the call's mic and speaker never moved away from
the ones it was set up on (checked every 10 s by the watcher and at stop).
Otherwise the raw mic is used. If the call app plays to a non-default
speaker, no canceller is started. `meta.json` says which track was used
(`mic_echo_cancel`, `mic_track_reason`). The WAVs are deleted only after
the encoded audio verified at full length.

`[whisper] initial_prompt` gives whisper a vocabulary line (names, product
terms) for both tracks; see `config.ini.example`.

## Development

```sh
make check    # py_compile + unittest suite
make test     # unittest suite only
```

The test suite (`tests/test_pure.py`) covers the pure functions only — no audio
and no faster-whisper import required.

## Bar widget, auto-record, output layout

See `NOTES.md` for the Omarchy bar widget and the `watch` service that
auto-records Teams calls, and `RECORDINGS.md` for the output folder layout
(`HHMMSS-<source>[-<title>]/` with `meeting.m4a`, `transcript.md`,
`transcript.txt`, `transcript.vtt`, `segments.json`, `meta.json`). Extra
commands: `status --json`, `auto on|off|toggle`, `watch`, `transcribe <folder>`,
`export <folder>...` (backfill `transcript.vtt` + `segments.json` for older
recordings; Teams-format WebVTT that FocusTime imports).

## Screen moments

While recording, a background `screens-loop` (needs `grim` + `hyprctl`) checks
the Teams window every 3 s and keeps a JPEG whenever a large part of it (one
connected region of at least 25 %) changes and then settles — a new slide, a
shared document — but not live video, speaking rings or name labels. Nothing
is captured while the window is narrower than `min_width` (900 px). After
transcription, `moments.md` pairs each frame with what was said around it,
and `meet-recorder-summarize` (also in this repo) tags action items/decisions
with `[mm:ss]` and links the matching frame (`link-screens`). Images never
leave the machine; only transcript text goes to the summarizer. Configure in
`[screens]` (see `config.ini.example`); `enabled = false` turns it off.
Commands: `moments <folder>...`, `link-screens <folder>...`.
