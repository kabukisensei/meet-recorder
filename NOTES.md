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
5. `master` is the shared branch — **GitHub (`kabukisensei/meet-recorder`)
   is canonical** (remote `origin` on the VPS clone). The SER9 pushes to the
   VPS bare repo (`vps-repo:/srv/meet-recorder.git`, remote `vps`), whose
   post-receive hook mirrors `master` to GitHub automatically. If you clone
   fresh, clone from GitHub.

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
- Devices: the recorders run with no `--target`, so they follow the default
  sink/source (verified: switching the default sink mid-recording re-links the
  capture). They are pinned only if the call app itself uses a non-default
  device (`app_device()`). Headphones therefore work, including mid-call swaps.
- Speaker labels: `stop` also saves `me.m4a` (mic) and `others.m4a` (sink).
  `transcribe` runs whisper on each and `merge_speakers()` interleaves them as
  `Me:` / `Others:`, dropping mic segments that are just speaker echo (word
  overlap with what the others said within 3 s). No per-person diarization.
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
