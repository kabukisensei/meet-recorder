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
- `install-hypr-bind.sh` — adds the Super+Alt+R snap binding (idempotent)

## Hard rules

Retention: `meet-recorder prune [--days N] [--apply]` archives the text
artifacts of meetings in date folders >= N days old into
`~/Recordings/.archive/<date>_<folder>.tar.gz` and deletes the meeting
folder (audio + screens) only after the archive reopens clean.
Invariants: only `YYYY-MM-DD` dirs under `output_dir` are considered;
symlinks and non-meeting subdirs are never deleted; date dirs are
rmdir'd only when empty; dry-run is the default.

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
6. **No audible tests, ever.** Never play sound to a real sink (pw-play,
   paplay, aplay, ffplay, speaker-test…): the user is at the desk. Only
   silent checks: pw-dump / wpctl / pactl inspection, recording (capture
   only), null sinks. Do not load/unload PipeWire modules or restart
   PipeWire while a call is live (`session.json` exists or Teams holds the mic).
7. **The recorder must never put desktop audio at risk.** Nothing is ever
   added to the PipeWire daemon / WirePlumber config; the echo canceller is
   a per-recording client process whose output is private (never a default,
   never listed to apps, never autolinked). See "Echo-cancelled mic" below.

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

## Echo-cancelled mic (2026-11-24; fix round 7 rework)

- Why: on speakers the mic hears the far side; me.m4a and the mix carried
  an echo and whisper sometimes gave their words to "Me" (merge_speakers
  only drops part of it). Priority: transcription accuracy over speed.
- Hard rule: the user's desktop audio must never be put at risk by the
  recorder. Commit 1548c0c's daemon drop-in (`pipewire/50-meet-recorder-aec.conf`)
  failed that (Codex BLOCKER/MAJOR) and is gone; install-local.sh deletes
  an installed copy (config only, nothing restarted).

### Design: one private canceller process per recording

- `start` (`start_aec`) writes `<raw dir>/aec.conf` (`aec_conf_text`) and
  runs `pipewire -c <raw dir>/aec.conf` detached: a separate PipeWire
  CLIENT process (the filter-chain.conf pattern; no `core.daemon`, so no
  server socket) holding module-echo-cancel. pid + start time are
  `aec_pid` / `aec_start` in session.json (not in `pids`). `stop`, stale
  recovery and start's rollback stop it AFTER the recorders
  (`stop_procs(helpers=…)` -> `stop_helpers`: SIGTERM, which quits its main
  loop, SIGKILL after 3 s; identity-checked like everything else).
  Env scrubbed (`aec_env`, fix round 8): every `PIPEWIRE_*` variable
  except `PIPEWIRE_REMOTE` / `PIPEWIRE_RUNTIME_DIR` is dropped --
  `PIPEWIRE_CONFIG_*` (another config), `PIPEWIRE_AUTOCONNECT` (forced
  autoconnect, stream.c `pw_stream_connect`), and `PIPEWIRE_PROPS` /
  `PIPEWIRE_NODE` / ... which stream.c applies AFTER the configured
  stream props (an inherited `media.class` would unhide our output).
- Nodes (per-session names, `aec_names`): `meet-recorder.aec-capture.<id>`
  (capture, `target.object` = the call's mic at start (`aec_mic` in
  session.json), `node.passive = false`), `meet-recorder.aec-reference.<id>`
  (monitor.mode: capture of the default sink's monitor, untargeted = same
  rule as the speaker recorder), `meet-recorder.aec-mic.<id>` (output).
- Smallest blast radius: nothing is loaded into the daemon or WirePlumber,
  no file under ~/.config/pipewire, and the node exists only while we
  record. A missing WebRTC plugin, a bad config or a crash ends only that
  process; the recording continues on the raw mic.

### Requirement 1 proof: a broken AEC can never stop PipeWire starting

PipeWire 1.6.9 `src/pipewire/conf.c` `load_module()`:
`if (pw_context_load_module(...) == NULL) { if (errno == ENOENT && flags
&& strstr(flags, "ifexists")) skip; else if (flags == NULL ||
strstr(flags, "nofail") == NULL) { pw_log_error("could not load mandatory
module"); return -errno; } else pw_log_info("could not load optional
module"); } return 0;`. `parse_modules()` breaks on `res < 0`,
`pw_conf_section_for_each()` returns the callback's result,
`pw_context_parse_conf_section()` returns it, and `context.c`
`pw_context_new()` does `if ((res = pw_context_parse_conf_section(this,
conf, "context.modules")) < 0) goto error_free;` -> NULL ->
`src/daemon/pipewire.c` `main()` "failed to create context", exits.
module-echo-cancel returns `-ENOENT` when `spa_plugin_loader_load()` of
`library.name` fails. So 1548c0c's drop-in (no flags) would have kept
the daemon from starting if the WebRTC plugin broke. Now:
1. the daemon config has no AEC entry at all (nothing to fail);
2. the helper's module entries carry `flags = [ ifexists nofail ]`
   (load_module returns 0), so even the helper keeps running; the
   recorder then sees no output node within `AEC_NODE_WAIT` (4 s),
   stops the helper and records raw.
`pipewire -c /abs/path.conf`: conf.c `get_abs_path()` uses an absolute
name directly (the `.conf.d` beside it is scanned only if it exists).

### Requirement 2 proof: the output is recorder-private

`source.props` sets `media.class = "Stream/Output/Audio/Internal"` (a
class WirePlumber itself uses for alsa split streams), `node.autoconnect
= false`. module-echo-cancel.c merges `source.props` BEFORE `if
(pw_properties_get(impl->source_props, PW_KEY_MEDIA_CLASS) == NULL) ...
"Audio/Source"`, so our class wins; the output stream is connected
without `PW_STREAM_FLAG_AUTOCONNECT`.
- Never a default source: WirePlumber 0.5
  `default-nodes/rescan.lua` collects candidates only with media.class in
  {Audio/Source, Audio/Source/Virtual, Audio/Duplex, Audio/Sink} (exact
  match, `"c"` constraint) -- ours is not one, in any device situation
  (real mic present, unplugged or swapped).
- Never a fallback target for anyone's mic: `linking/find-best-target.lua`
  only considers `item.node.type = "device"`; `node/create-item.lua` makes
  every `Stream/*` class a "stream". `find-default-target` uses the default
  node (above); smart-filter and role targets need properties we do not set.
- Never linked by WirePlumber on its own (so never played to a speaker):
  `linking/rescan.lua` skips items whose `node.autoconnect` is not true.
- Never offered to apps: pipewire-pulse `manager.c`
  `pw_manager_object_is_source()` = media.class exactly Audio/Source,
  Audio/Duplex or Audio/Source/Virtual; `..._is_sink_input()` exactly
  Stream/Output/Audio. Ours matches neither, so Teams/pavucontrol never
  list it (not as a mic, not as a playback stream).
- How we record it: pw-record `--target meet-recorder.aec-mic.<id>`
  (target.object) with `node.dont-fallback = true`;
  `find-defined-target.lua` matches any linkable by node.name with the
  right direction, so only a client naming it links to it. dont-fallback:
  if the node vanishes the recorder is destroyed instead of silently
  recording the default mic (a gap, so finalize uses raw).
- Enforced at runtime too (`aec_graph_state` over pw-dump, before and
  after linking): if the output node shows any other media.class or a link
  to anything but our recorder, the helper is stopped at once and the
  session records raw. Belt and braces: `priority.session/driver = 1`.
- The capture/reference streams are ordinary Stream/Input/Audio nodes of
  our helper (source-outputs in pactl); `call_streams` ignores
  `meet-recorder.*` names, so the watcher never mistakes them for Teams.

### Requirement 3: reference correctness

`plan_mic`: AEC only if the call app plays to the default sink
(`pin_sink` empty). Then the reference and the speaker recorder are both
untargeted `stream.capture.sink` captures and WirePlumber links (and
re-links on a default change) both to the default sink. Teams on a
non-default sink -> raw mic (on headphones there is no echo anyway). A
pinned MIC is fine: the canceller captures that same mic by name.

### Requirement 4: AEC is strictly additive (fix round 8, Codex round 7)

- The raw mic recorder is exactly the pre-AEC one (`plan_mic` target =
  pin_mic or the default; it follows a default-mic switch like Teams
  does) and writes `mic.wav`. AEC is an extra track, `mic-aec.wav`.
- Start order (`start_locked`): speaker + raw recorders FIRST (origin),
  0.4 s check, THEN the canceller (`start_aec`) with one total budget
  `AEC_BUDGET` = 3 s for node + link; every pw-dump is bounded by the time
  left (`wait_aec_graph(deadline=...)`, `run_json(timeout=)`). Failure =
  helper/recorder stopped (1 s each), WAV deleted, `mic_note` says why;
  the primaries never notice. `aec_delay` (session.json) = link time -
  origin: a complete AEC track is raw - aec_delay long.
- Primary failures never discard usable tracks: whichever of speaker /
  raw mic came up is recorded (`start_note`, notified); AEC is skipped
  without the raw mic. Neither up -> `StartFailed` -> RETRY (76), so the
  watcher retries (bounded) instead of marking the call handled; manual
  starts are notified at once.
- Route divergence (`aec_route_error`, pure): module-echo-cancel sets
  `node.link-group`, so WirePlumber never moves its capture with the
  default (Codex). AEC is invalid once the call's mic != `aec_mic` or the
  call app plays to a non-default sink (the reference follows the
  default). With no call stream, the mic is compared with the default
  only if `aec_mic` was the default at start (`aec_follows_default`).
  Checked by the watcher every `AEC_ROUTE_SECONDS` (10 s; first hit
  recorded as `aec_diverged` in session.json under the lock,
  `mark_aec_diverged`) and once more at stop (`probe_aec_route`, pactl
  bounded to 2 s each). Unreadable defaults count as divergence.
- Choice (`mic_choice` -> `pick_mic_track`, pure, over `wav_profile` =
  duration + 1 s peaks + 1 s RMS via `math.sumprod`): AEC only if (a) not
  diverged; (b) both readable 16-bit; (c) lengths agree: raw - AEC within
  0.5 s of `aec_delay` both ways, delay <= `AEC_MAX_DELAY` (5 s); (d)
  `aec_voice_error` finds no lost voice: AEC wholly near-silent (RMS < 30)
  while raw has speech (1 s RMS >= 250) anywhere -> raw; otherwise per
  10 s window with raw speech-level seconds (fix round 10, Codex: overlap
  used to be skipped, so AEC could erase the user talking over the far
  side): seconds with sys.wav quiet (RMS < 100) are lost if AEC is
  near-silent (< 30 or 20 dB under raw); seconds with the far side
  audible are lost if AEC < `AEC_OVERLAP_RATIO` (0.25) x raw; a window
  is lost if either group is; > 20 % of windows lost -> raw. RMS cannot
  tell double-talk from loud echo, so loud echo that AEC rightly removed
  also rejects AEC -- deliberate: when in doubt, raw. The chosen AEC track is spliced behind the first offset
  seconds of raw (`splice_mic` -> `mic-aec-aligned.wav`) so it lines up
  with sys.wav. Fix-round-7 sessions (AEC in `mic_wav`, raw in
  `mic_raw_wav`) use their raw track.
- Verification (`encode_session`): meeting.m4a, me.m4a and others.m4a
  must all decode at >= 95 % - 1 s of their input, on the AEC path AND
  the raw path. Stems are encoded to `.<name>.partial.m4a` and renamed
  into place only once verified (old stems are removed first), so an
  existing stem is a verified one (fix round 10). `transcribe_inputs`:
  per-track transcription only when BOTH stems exist and meta.json has
  no `audio_unverified`; otherwise meeting.m4a, unlabelled (like old
  recordings; `speaker_labels: false`). AEC failing -> re-encode from raw. Raw failing ->
  `Unverified`: every WAV is kept in the staging dir, meta.json gets
  `audio_unverified` + `wavs_kept_in`, transcription still runs (the
  collector keeps a dir with WAVs).
- meta.json: `mic_echo_cancel`, `mic_track_reason` (AEC sessions) or
  `mic_note`, `start_note`.

### Requirement 5

`[audio] mic_source` defaults to `raw` (fix round 8): AEC is opt-in with
`auto` until it has passed a real call. `raw` = no canceller process at
all. Blank = raw.

### Still to verify live (silently, see the fix-round-7 commit)

stream-to-stream link of our pw-record to the private node; the AEC track
passing pick_mic_track on a real call; reference re-link on a default
sink change mid-call; the voice-check thresholds (RAW_SPEECH_RMS etc.)
against a real mic. Until then `mic_source = raw`.

- Whisper: `[whisper] initial_prompt` (raw read, `%` ok; blank = none),
  `beam_size` (>= 1), `condition_on_previous_text` -> `whisper_options`,
  validated before the model loads, same options for both tracks.

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
- `start` spawns a detached hidden `screens-loop <raw-dir> <session-id>
  <origin>` (if `[screens] enabled`, default true); it leads its own
  process group. Its pid + kernel start time are `screens_pid` /
  `screens_start` in session.json (NOT in `pids`, so a lingering loop never
  makes the session look live). Frame offsets are `time.monotonic() -
  origin`; origin is taken right before pw-record starts (after the slow
  device probes), wall time only names the folder.
- The loop exits by itself (checked every ~1 s, 3 misses) when its raw dir
  is gone, session.json no longer has its `id`, or no audio recorder is
  alive any more. hyprctl/grim run one at a time with hard timeouts (5 s /
  10 s); SIGTERM kills the running one at once (also one spawned while the
  handler ran). Each gets `PR_SET_PDEATHSIG = SIGKILL`, so it dies with the
  worker even if the worker is SIGKILLed or crashes (the timeouts are
  enforced by the worker, so they alone would not bound an orphan).
  Full frames come from `grim … -` (stdout) and are written by us 0600, so
  there are never grim-made partial files.
- Disk: a frame is skipped (logged) past `max_mb` (300) of frames or when the
  disk would drop below `min_free_mb` (1024, the WAVs share the disk).
  screens.log is best effort and stops growing at 256 KiB. A keep only
  counts after the JPEG and its manifest line are both written (the line is
  one append; a short write is truncated away).
- Privacy: the raw session dir (WAVs + frames) and `<folder>/screens/` are
  0700; frames, screens.jsonl, screens.log, screens.json are 0600.
- Each tick (`interval_seconds`, 3): `hyprctl clients -j` → pick the window
  (`pick_screen_window`: class substring, mapped, meeting-like title, else
  largest) → `grim -T id -s <scale> -t ppm -` thumbnail 200 logical px wide
  (~250x140 px on this 1.25x display), parsed in plain Python (`parse_ppm`)
  and compared per cell of a 12x12 grid (`cell_diffs`: mean absolute byte
  difference per cell, 0–1; ~3 ms per diff).
- **Volatile mask** (why: a real Teams share has a strip of live camera
  tiles beside/under the slide; one global diff never settled → zero frames).
  Keep rule (`screen_keep_step`, pure, state = last `volatile_window` "moved"
  bitmaps + mask): a cell *moved* if its diff to the previous tick >
  `stable_threshold` (0.02). A cell is *volatile* once it moved in ≥
  `volatile_ticks` (2) of the last `volatile_window` (5) ticks and stays so
  until it has not moved for 5 ticks (hysteresis, so low-motion video does
  not flicker in and out). Volatile cells are ignored: *settled* = no calm
  cell moved for `settle_ticks` (2) ticks; *new* = `region_is_new` (fix
  round 8, below). > `max_volatile` (85 %) volatile (full-window video,
  gallery) → nothing kept. Then `min_gap_seconds` / `max_frames`. A failed
  sample, a missing window or a different stableId resets the history (no
  stale diff).
- **Large coherent region** (fix round 8; why: two real calls kept 39
  frames / 29 min and 55 / 46 min, almost all Teams gallery views --
  speaking rings, tile reflow, name labels). *new* = the calm cells that
  differ from the last kept thumbnail by > `cell_change_threshold` (0.02)
  contain ONE region of >= `region_fraction` (25 %) of ALL cells
  (`largest_region`: cells within Chebyshev distance 2 are connected,
  `SCREEN_REGION_REACH`, so a slide's text lines with blank rows between
  them are one block; strict 8-connectivity gave the synthetic text deck
  34-45 cells, right at the 36-cell line). Operator's numbers
  (`.work/frame-stats.txt`, consecutive kept frames): gallery churn changed
  <= 20 % of cells, largest connected <= 18 %; shares/slide changes mostly
  one block of 25-95 %. Proxy on today's kept frames: call 1 39 -> 7
  (lower bound 7, upper 8 if every changed cell joined), call 2 55 -> 28
  (28-31). A single new text line or caret is now NOT a new frame; a
  first frame is still kept at the start (nothing to compare with).
  Real-number test: `test_real_call_numbers` (layout model: largest region
  as one block, the rest as separate clumps).
- **min_width** (fix round 8): no capture while the window is narrower
  than `min_width` (900 logical px; call 1's Teams window was 338-598 px
  wide, so its share was unreadable) -- logged once per worker. Call 1
  would have produced 0 frames.
  Why 2 (not the 3 first tried): live, a random strip cell sometimes holds
  still for one tick; 3-of-5 then took ~15 s to warm up and missed a first
  slide shown for 15 s (measured).
- **Transitions are not video** (fix round 4, Codex): a slide change caught
  on 2 ticks (a fade, a half-drawn repaint) moved the same cells twice →
  masked; with a slide every 15 s (2 moving + 3 still ticks) the 5-still
  release never came and every later slide was lost. Now a tick on which ≥
  `burst_fraction` (10 %) of all cells *start* moving (calm, and still for
  the previous `SCREEN_BURST_QUIET` = 2 ticks) is a burst: those cells' moves
  on it and on their next `transition_ticks - 1` (1) ticks are not
  volatility evidence. Video moves continuously, so it still masks (a share
  that starts together with a live strip is masked ~2 ticks later). Why
  "still for 2 ticks": a strip cell pausing for one tick then moving again
  is not an onset (that broke warm-up when "previous tick" alone was used).
  The first tick after a reset is never a burst, so warm-up is unchanged.
  Up to 3-tick transitions keep every slide (tested); a small change
  (< 10 % of cells) seen on 2 ticks is still masked and kept ≤ 5 ticks later.
- **The operator's live "1 frame at 9.5 s" was the test window**:
  `/tmp/mr-slides.py` does `rows, cols = os.get_terminal_size()`, but that
  is `(columns, lines)`, so it wrote ~67 lines of 30 chars into a 30-line
  terminal and every redraw scrolled the slide off the top; only the strip
  was ever on screen (verified from the captured thumbnails). With that one
  line fixed (and started 1 s after foot, so Hyprland's tiling has settled
  the size), the real loop keeps one frame per slide.
  Consequences: a slide with a live strip is kept ~2 ticks after the strip
  turns volatile (first slide ~9 s in) or after the slide changes (~6 s);
  a camera-only call with static Teams chrome (< 85 % video) keeps one frame
  at the start; a camera that stops moving makes its cells eligible again
  after 5 ticks (one extra frame if that changed the picture).
- Thresholds: an unchanged window diffs to exactly 0.0 (deterministic
  compositor), so any per-cell value only has to beat animation. A changed
  text line moves its cells by ~0.1–0.5 (18 of 108 calm cells in the test
  slide); a caret-sized change touches one cell (<1 %) and is ignored.
- Kept: full-res `grim -T id -t jpeg -q 80 -` → `<raw>/screens/00123.4s.jpg`
  (offset from start, sortable) + a line in `<raw>/screens.jsonl`. Errors
  go to `<raw>/screens.log` (deduplicated), never crash the loop. The loop
  runs at nice 10.
- Cost: one grab ≈ 20–30 ms of grim; the loop averages well under 1 % of a
  core (see the commit's live proof).
- `finalize_audio` only mixes audio and deletes the WAVs; `stop` then spawns
  `transcribe` AND a separate detached `collect-screens <raw> <folder>` job,
  so slow storage never delays the audio or the transcription.
  `finalize_audio` never reads screen data (a bad manifest once could fail
  it after the WAVs were deleted); a collector spawn error is logged only.
  The collector copies each frame (0600, read back and compared), publishes
  `screens.json` listing exactly the verified copies, merges `"screens": N`
  into meta.json. It refuses paths that are not <staging dir directly under
  `~/.cache/meet-recorder`, no meta.json> → <recording folder with meta.json,
  outside the cache> (`collect_paths_error`; swapped arguments delete
  nothing, the CLI exits 2). An unreadable manifest is an error, not "no
  frames". It stops copying before the output disk drops below
  `min_free_mb`. The raw dir is deleted only if all of that went fine, every
  `NNNNN.Ns.jpg` in it was copied, and it holds nothing but `screens/`,
  `screens.jsonl`, `screens.log` (a leftover WAV keeps it); otherwise re-run
  `meet-recorder collect-screens <raw> <folder>`. The collector also
  removes frame-less raw dirs (screens off) since finalize no longer does.
  meta.json is merged under a folder flock (`update_meta`) by both jobs;
  transcript.md/.txt are now written atomically.
- `moments.md` (frame image + the segments overlapping [t-30 s, t+15 s]) is
  written by whichever of transcribe / collect-screens finishes last
  (`moments_if_ready`, under the folder lock); rebuild with
  `meet-recorder moments <folder>`. If collection finishes after the summary
  ran, re-run `meet-recorder link-screens <folder>`.
- `link-screens` only treats a `[mm:ss]` / `[h:mm:ss]` that ENDS the line
  (optional `.`, not a Markdown link) as a tag; `read_frames` only returns
  manifest entries with a finite `t`, a `NNNNN.Ns.jpg` basename and an
  existing file.

## Session lifecycle (2026-11-23 fix round)

- `start`, `stop`, `toggle` and the watcher's stale-session recovery all run
  under `flock(~/.local/state/meet-recorder/lock)` (`lifecycle_lock`,
  re-entrant in-process, "busy" after 120 s). `start` publishes session.json
  ONCE, complete (recorders verified, worker attached, unique `id`),
  atomically, 0600. `stop` signals + clears state under the lock, then mixes
  outside it.
- Process identity: session.json stores each pid's kernel start time
  (`pid_starts`, `screens_start`; /proc/<pid>/stat field 22). A pid whose
  start time differs (PID reuse) or that is a zombie is dead; signals go via
  pidfd after the identity check. Signalling errors are printed, never
  raised, so finalization always runs.
- `stop_procs`: recorders SIGINT, worker SIGTERM, wait 5 s, SIGKILL
  survivors -- only those identity-checked pids; a worker with no saved
  start time is never signalled (logged; fix round 6). Fix round 5 removed the
  process-group enumeration (`group_members`): a group id can be reused
  between any check and the scan, so it could authorize signals to an
  unrelated process (Codex). Capture children are instead bound to the
  worker's lifetime (PDEATHSIG, above). The watcher
  reaps its children every poll and recovers a session whose recorders
  died as soon as it notices.
- Lock scope (fix round 4): audio mixing never runs under the lock. `stop`,
  `toggle` and stale recovery stop + clear under it and call
  `finish_session` after releasing it (a crashed session found by `start`
  is mixed after the new recording started). Commands run under it are
  bounded (`pactl get-default-*` 5 s, `run_json` 5 s, `notify-send` 5 s).
  Busy = exit 75 (`BUSY`); the watcher retries a busy start/stop next poll
  instead of marking the call handled. A `pactl get-default-*` that times
  out, fails or prints undecodable output is transient: start exits 76 (`RETRY`) and the watcher
  retries every poll while the call lasts, at most `START_RETRIES` (10)
  times / `START_RETRY_SECONDS` (60 s; checked before each attempt, so
  none launches after 60 s), then notifies "could not start
  recording" and leaves the call alone (fix round 5; one timeout used to
  skip the whole call). A manual start notifies at once.
- A crashed session that start/toggle recovered is mixed in a `finally`
  (`finish_stale`), so a new start that fails (ENOSPC on the staging dir)
  or raises never strands it; the staging dir is created inside start's
  guarded block, so that failure is a clean exit 1. The staging dir is
  made exclusively (`new_session_dir`: `os.mkdir`, a new name on EEXIST),
  so start's rollback only ever deletes a dir it created (fix round 6).
  Not persisted across
  process death (if start itself is killed mid-way, the WAVs stay in
  `~/.cache/meet-recorder/<id>/`).
- The watcher stops by identity: `cmd_stop(expect_id=…, expect_source=
  "teams")` re-checks both under the lock and stops nothing if a manual
  recording replaced its Teams one in the meantime.
- Start resolves every setting (`start_settings`: sample rate, apps,
  `[screens] enabled`, which tolerates `enabled = %`) and probes the
  devices before spawning; from the first spawn to the published state is
  one try block: any exception (incl. KeyboardInterrupt) stops what was
  spawned and removes the raw dir. (A SIGKILL mid-start can still strand
  recorders; out of scope.)
- `meet-recorder auto on|off` rewrites only `[auto] enabled` in config.ini
  (it used to freeze every default into the file).
- `meet-recorder latest` prints the newest folder with a transcript.md under
  `[paths] output_dir`; the summarizer's no-argument mode uses it.
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
- With speakers (not headphones) the mic also hears the far side. Fixed by
  the echo-cancelled mic (above) when its track passes pick_mic_track;
  otherwise the mix has a slight echo and merge_speakers drops only part
  of it (`mic_track_reason` in meta.json says why).

- VAD hallucinates on sparse/short audio — a few seconds of speech may
  transcribe to nonsense. Continuous meeting audio is fine.
- The shell-out probe for the bar widget uses `sed`/`date` in `sh`, not QML
  JS math (QML `Date.now()` bindings proved unreliable in this shell).
- `SUPER+SHIFT+R` is the user-facing toggle (wired in `~/.config/hypr/bindings.lua`).
- `SUPER+ALT+R` = `meet-recorder snap` (Task 9, below). Verified free on
  2026-11-24 with `hyprctl binds -j` (modmask 72 + R unused; SUPER+CTRL(+ALT/
  SHIFT)+R are the reminder binds).

## Known limitations

- The speaker capture follows the system default sink. If Teams alone is
  switched to a different output device while the system default stays
  put, the remote side is not captured. Switch the system output (e.g. via
  the bar / AirPods panel) rather than inside Teams.
- The snap hotkey collision check (`install-hypr-bind.sh`) covers keysym
  binds only, not `code:NN` keycode binds (verified 2026-11-24: no keycode
  binds exist on this box).

## Manual snap (Task 9, 2026-11-24)

- `meet-recorder snap [--window focused]` (`cmd_snap` / `take_snap`): if
  session.json is live, picks the window like screens-loop
  (`pick_screen_window`, `[screens] window_class`) or the active one
  (`hyprctl activewindow -j`, stableId looked up in `clients` if absent),
  grabs a full-res JPEG via `grab_frame` (grim -T, stdout), writes it 0600
  into `<raw>/screens/` (0700) under a free `NNNNN.Ns.jpg` name (moved on
  by 0.1 s if taken, so it never replaces an automatic frame) and appends
  `{"t","file","window_title","w","h","manual": true}` to screens.jsonl
  (one O_APPEND write, like the loop). Bypasses the keep rule, min_gap,
  min_width and settling; respects max_mb / min_free_mb
  (`frame_budget_error`). Not live -> exit 1.
- Visual signal: `notify_snap` -> `notify-send -a "Meeting snap" -c
  meet-recorder.snap -t 2500 -i <jpg> -h string:image-path:<jpg> "Screen
  saved" "mm:ss · <title>"`; failure: `-u critical -t 6000 "Screen NOT
  saved — <reason>"`. Mako can style it by app-name/category.
- `status --json` gains `snaps` / `last_snap_unix` (`session_snaps`: manual
  entries in the live manifest; unix = start + t). Panel.qml flashes
  `● MM:SS  <camera> N` for 2 s when `snaps` rises between polls (1 s while
  recording); -1 = unknown after a shell restart, so no false flash.
- Downstream: the collector copies entries verbatim (screens.json keeps
  `manual`); `render_moments` appends "📌 marked by you"; `pick_frame`
  prefers a manual frame anywhere in [t-90 s, t+15 s] (same latest-before /
  earliest-after rule among the manual ones).
- Hotkey: `install-hypr-bind.sh` (called by install-local.sh) inserts
  `o.bind("SUPER + ALT + R", "Meeting snap", "meet-recorder snap")` right
  after the toggle line only if no `meet-recorder snap` binding exists;
  SUPER+SHIFT+U if `hyprctl binds -j` shows modmask 72/R taken (65/U too ->
  warn, nothing added). `hyprctl reload` only after a change; non-blank
  `hyprctl configerrors` (clean output is just newlines) -> restore the
  backup, reload, warn. `HYPRCTL=` overrides for tests.
