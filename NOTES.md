# meet-recorder — working notes for Claude Code

Local meeting recorder for the SER9 (Omarchy/Hyprland). Records the default
sink monitor + mic via `pw-record`, mixes with ffmpeg, transcribes locally
with faster-whisper. Zero network/cloud use at runtime.

## Repo layout

- `meet-recorder` — the CLI (stdlib-only Python, ~300 lines, plain functions)
- `meet-recorder-wrapper` — venv launcher installed as `~/.local/bin/meet-recorder`
- `tests/` — pure unit tests; `make check` runs them
- `omarchy-widget/` — Quickshell bar indicator (`● REC MM:SS`, click to stop)
- `install-local.sh` — syncs this repo to the installed locations

## Hard rules

1. **The venv `~/venvs/meet-recorder/` is read-only.** PyAV is pinned to
   `av==15.1.0`: av>=16 removes the `metadata_errors` kwarg faster-whisper
   1.2.1 needs; av<=14 never had it. Never `pip install -U av`.
2. **Never edit installed copies directly** (`~/.local/bin/meet-recorder*`,
   `~/.config/omarchy/plugins/jennings.meetrecorder/`). Edit here, then run
   `./install-local.sh` (it runs `make check` first and refuses on failure).
   The Omarchy shell hot-reloads the widget file after sync.
3. **No secrets in this repo, ever.**
4. Config defaults live in `meet-recorder`; `~/.config/meet-recorder/config.ini`
   overrides (`model`, paths). `config.ini.example` shows the shape.
5. `master` is the shared branch — `git push origin master` syncs the VPS
   canonical repo (`/srv/meet-recorder.git`).

## Test

- Fast: `make check`
- Live: `meet-recorder start`, wait, `meet-recorder stop` — watch for the
  finalize + transcription notification; output lands in `~/Recordings/`.
  The bar shows a red `● REC MM:SS` while recording (polls state once/sec).

## Known quirks

- VAD hallucinates on sparse/short audio — a few seconds of speech may
  transcribe to nonsense. Continuous meeting audio is fine.
- The shell-out probe for the bar widget uses `sed`/`date` in `sh`, not QML
  JS math (QML `Date.now()` bindings proved unreliable in this shell).
- `SUPER+SHIFT+R` is the user-facing toggle (wired in `~/.config/hypr/bindings.lua`).
