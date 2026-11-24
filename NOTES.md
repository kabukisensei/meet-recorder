# meet-recorder — working notes for Claude Code

Local meeting recorder for the SER9 (Omarchy/Hyprland). Records the default
sink (speaker side) + mic via `pw-record`, mixes with ffmpeg, transcribes locally
with faster-whisper. Zero network/cloud use at runtime.

## Repo layout

- `meet-recorder` — the CLI (stdlib-only Python, ~300 lines, plain functions)
- `meet-recorder-wrapper` — venv launcher installed as `~/.local/bin/meet-recorder`
- `meet-recorder-summarize` — optional agy summarizer (`[summary] summary_cmd`),
  installed to `~/.local/bin/`
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

## FocusTime export

- Every transcription also writes `transcript.vtt` (Teams-style WebVTT:
  `HH:MM:SS.mmm` timings, `<v Name>text</v>` cues, `&<>` escaped) and
  `segments.json` (exact float timings for later features). FocusTime's
  Calls drop zone imports the `.vtt` as if Teams made it.
- Voice names come from `[speakers] me` / `others` (defaults `Me` / `Others`,
  read raw so `%` is fine; blank falls back to the default);
  unlabelled segments (single-track / old recordings) use `others`.
- Cue end is always > start: both ends are rounded to ms, then
  `end = max(end, start + 1 ms)` (WebVTT rule). `segments.json` keeps the
  original float timings untouched.
- transcript.md/.txt are written first; a VTT/segments.json failure does not
  fail transcription — it is notified/logged; retry with `export`. If the
  segments.json refresh fails, the old segments.json is deleted (it would
  describe the previous transcript).
  VTT and JSON are published atomically (`write_bytes_atomic`: unique temp
  file + `os.replace`, UTF-8). Modes: a new file gets 0666 & ~umask (like
  `open()`), a replaced file keeps its mode, `private=True` forces 0600.
- `meet-recorder export <folder>...` backfills old folders (`folder_segments`):
  `segments.json` is used only if valid, non-empty AND not older (mtime) than
  `transcript.md`; otherwise `transcript.md` is parsed (second precision;
  continuation lines of a multi-line utterance are kept) and segments.json
  rewritten. A torn/invalid segments.json with no transcript.md is an error.
  Times beyond ±1e7 s (`MAX_SEGMENT_SECONDS`) are invalid (no OverflowError).
  Speaker prefixes: `meta.json`
  `speaker_labels: true` → `Me: ` / `Others: ` are labels; meta.json without
  it → no labels (text kept whole); no meta.json → prefix heuristic. A
  transcript with no `[mm:ss -> mm:ss]` rows is an error unless it is exactly
  the recorder's no-speech body (`# title`, `Duration:`, `_No speech
  detected._`, nothing else); a row with an impossible time (`[00:75 -> …]`)
  is an error, never continuation text. Bad folders are reported, the batch
  continues, exit 1 if any failed. Idempotent.
- Voice names are escaped once; cues sort by the unrounded start/end.
- Validate: `ffmpeg -y -v error -i transcript.vtt -f srt /dev/null` (without
  `-y` ffmpeg refuses to "overwrite" /dev/null and still exits 0).

## Screen moments (2026-11-23, Tanner's idea)

- `grim -T <stableId> out.png` captures ONE window by its Hyprland `stableId`
  (`hyprctl clients -j`), **even when its workspace is not visible**. That is
  what makes this work without recording video. Teams class: `teams-for-linux`.
- `start` spawns a detached hidden `screens-loop <raw-dir>` (if `[screens]
  enabled`, default true); its pid is `screens_pid` in session.json (NOT in
  `pids`, so a lingering loop never makes the session look live). `stop`
  terminates it together with the recorders (same SIGINT→wait→SIGKILL). The
  loop also exits by itself when the raw dir is gone or session.json no
  longer names its raw dir (2 ticks in a row), so it cannot orphan.
- Each tick (`interval_seconds`, 3): `hyprctl clients -j` → pick the window
  (`pick_screen_window`: class substring, mapped, meeting-like title, else
  largest) → `grim -T id -s <scale> -t ppm -` thumbnail ~160 logical px wide
  (~200 px on this 1.25x display), parsed in plain Python (`parse_ppm`) and
  compared by mean absolute byte difference (`thumb_diff`, 0–1).
- Keep rule (`screen_keep_step`, pure): keep when the diff to the last kept
  frame > `change_threshold` AND the diff to the previous tick was <
  `stable_threshold` for `settle_ticks` ticks in a row; plus `min_gap_seconds`
  and `max_frames`. Kept: full-res `grim -T id -t jpeg -q 80` →
  `<raw>/screens/00123.4s.jpg` (offset from start, sortable) + a line in
  `<raw>/screens.jsonl`. Errors go to `<raw>/screens.log` (deduplicated),
  never crash the loop. The loop runs at nice 10.
- Thresholds (measured 2026-11-23 on this machine): an unchanged window gives
  a diff of exactly 0.0 (the compositor output is deterministic), so
  `stable_threshold = 0.002` only has to absorb small animations (speaking
  rings, a cursor) — live camera video is far above it and never settles.
  Text-only slide changes in a small tiled terminal measured 0.003–0.007;
  real slides / doc pages differ far more. `change_threshold = 0.005` (2.5x
  the stable band so a settled frame cannot flap) catches those while
  ignoring a blinking caret or a reaction pop-up.
- Cost: one grab ≈ 20–30 ms of grim; the loop averages well under 1 % of a
  core (see the commit's live proof).
- `finalize_audio` moves frames to `<folder>/screens/`, writes `screens.json`
  (array, tolerant of a torn last jsonl line) and `meta.json "screens": N`
  before deleting the raw dir. A screens failure never costs the audio.
- `transcribe` writes `moments.md` after the transcripts (frame image + the
  segments overlapping [t-30 s, t+15 s], from the precise segment times);
  rebuild with `meet-recorder moments <folder>`.
- `meet-recorder-summarize` lives in this repo now (installed by
  install-local.sh). Its prompt asks for `[mm:ss]` at the end of each action
  item / decision; afterwards it runs `meet-recorder link-screens <folder>`,
  which appends ` — [screen mm:ss](screens/<file>)` using the latest frame in
  [t-90 s, t], else the earliest in (t, t+15 s]. Idempotent. **Images are
  never sent to agy**, only the transcript text.
- Testing without touching the real config: `MEET_RECORDER_CONFIG=<file>`
  replaces `~/.config/meet-recorder/config.ini` (children inherit it); point
  `[screens] window_class` at a test window (e.g. `foot --app-id mr-screen-test`).

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
