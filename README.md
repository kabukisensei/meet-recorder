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
sys_volume = 1.0       # gain applied to system/meeting track
mic_volume = 2.0       # gain applied to mic track
sample_rate = 16000    # capture + mix sample rate (Hz)
mic_source = auto      # auto | raw | <PipeWire node.name> (see below)

[whisper]
model = small          # faster-whisper model size
device = cpu
compute_type = int8
beam_size = 5
initial_prompt =       # vocabulary/context line: names, product terms
condition_on_previous_text = true

[paths]
output_dir = ~/Recordings
```

Edit it to taste; a partial file is merged over the defaults, so you only need
to list the keys you want to change. `~` in paths is expanded.

### Echo-free mic track

With speakers, the mic also hears the far side. `pipewire/50-meet-recorder-aec.conf`
(installed by `install-local.sh` to `~/.config/pipewire/pipewire.conf.d/`) adds an
echo-cancelled copy of the default mic, `meet-recorder.aec-mic`, using the
monitor of the default sink as the echo reference. It is used only by the
recorder. It never becomes the default source, so Teams keeps the raw mic and
does its own echo cancellation. The drop-in takes effect after the next login, or after
`systemctl --user restart pipewire pipewire-pulse wireplumber` (not during a
call). With `mic_source = auto` the recorder uses it when it is loaded. It also
records the raw mic as a fallback, used if the echo-cancelled track turns out
silent or stalled. Otherwise it records the default mic as before. `meta.json`
says which one was used (`mic_echo_cancel`).

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
the Teams window every 3 s and keeps a JPEG whenever its content changes and
then settles — a new slide, a shared document — but not live video. After
transcription, `moments.md` pairs each frame with what was said around it,
and `meet-recorder-summarize` (also in this repo) tags action items/decisions
with `[mm:ss]` and links the matching frame (`link-screens`). Images never
leave the machine; only transcript text goes to the summarizer. Configure in
`[screens]` (see `config.ini.example`); `enabled = false` turns it off.
Commands: `moments <folder>...`, `link-screens <folder>...`.
