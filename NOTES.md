# meet-recorder — working notes for Claude Code

Local meeting recorder for the SER9 (Omarchy/Hyprland). Records the default
sink (speaker side) + mic via `pw-record`, mixes with ffmpeg, transcribes locally
with faster-whisper. Zero network/cloud use at runtime.

## Repo layout

- `meet-recorder` — the CLI (stdlib-only Python, ~300 lines, plain functions)
- `meet-recorder-wrapper` — venv launcher installed as `~/.local/bin/meet-recorder`
- `tests/` — pure unit tests; `make check` runs them
- `omarchy-widget/` — Omarchy shell bar widget (`Panel.qml`): always-visible mic icon,
  red `● MM:SS` while recording, click for the start/stop panel
- `systemd/meet-recorder-watch.service` — user service running `meet-recorder watch`
  (auto-records Teams calls; enabled under graphical-session.target)
- `RECORDINGS.md` — installed as `~/Recordings/README.md`; documents the output layout
- `install-local.sh` — syncs this repo to the installed locations

## Hard rules

1. **The venv `~/venvs/meet-recorder/` is read-only.** PyAV is pinned to
   `av==15.1.0`: av>=16 removes the `metadata_errors` kwarg faster-whisper
   1.2.1 needs; av<=14 never had it. Never `pip install -U av`.
2. **Never edit installed copies directly** (`~/.local/bin/meet-recorder*`,
   `~/.config/omarchy/plugins/jennings.meetrecorder/`). Edit here, then run
   `./install-local.sh` (it runs `make check` first and refuses on failure).
   It also restarts the watch service and the Omarchy shell (hot-reload kept
   serving the old compiled QML, so the script restarts the shell).
3. **No secrets in this repo, ever.**
4. Config defaults live in `meet-recorder`; `~/.config/meet-recorder/config.ini`
   overrides (`model`, paths). `config.ini.example` shows the shape.
5. `master` is the shared branch — `git push origin master` syncs the VPS
   canonical repo (`/srv/meet-recorder.git`).

## Test

- Fast: `make check`
- Live: `meet-recorder start`, wait, `meet-recorder stop` — watch for the
  finalize + transcription notification; output lands in `~/Recordings/`.
  The bar shows a red `● MM:SS` while recording (polls `status --json`).
- Auto-record without a real call: `timeout 30 parec --client-name=teams-sim >/dev/null`
  — the watcher should start within ~6 s and stop ~20 s after parec exits.

## How it works (2026-11-23 rework)

- `start` spawns two detached `pw-record`s. The speaker side MUST use
  `-P '{ stream.capture.sink = true }' --target <sink>`. Targeting
  `<sink>.monitor` does not exist as a PipeWire node and silently records the
  mic instead (the original bug: both tracks were the mic).
- `stop` mixes to `~/Recordings/YYYY-MM-DD/HHMMSS-<source>[-<title>]/meeting.m4a`,
  writes `meta.json`, then spawns a detached `transcribe <folder>` so nothing
  (bar reload, service restart) can kill transcription.
- `watch` polls `pactl list source-outputs`; an app matching `[auto] apps`
  (default `teams`) holding the mic open == live call. It only stops sessions
  it started (`source: teams`), and won't restart one you stopped by hand
  until the next call.
- State: `~/.local/state/meet-recorder/` — `session.json` (live recording),
  `transcribing/` (job markers), `last.json`, `watch.json` (daemon heartbeat).
- Default whisper model is `small` (~25x realtime on this CPU). `medium` is
  more accurate and still practical; set `[whisper] model` in config.ini.

## Known quirks

- The Teams match was verified with a simulated mic client, not yet with a
  real Teams call. If a real call does not trigger, run
  `pactl list source-outputs` during the call and adjust `[auto] apps`.
- With speakers (not headphones) the mic also hears the far side, so the mix
  has a slight echo of it. Transcripts are fine.

- VAD hallucinates on sparse/short audio — a few seconds of speech may
  transcribe to nonsense. Continuous meeting audio is fine.
- The shell-out probe for the bar widget uses `sed`/`date` in `sh`, not QML
  JS math (QML `Date.now()` bindings proved unreliable in this shell).
- `SUPER+SHIFT+R` is the user-facing toggle (wired in `~/.config/hypr/bindings.lua`).
