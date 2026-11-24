# Meeting recordings

Everything `meet-recorder` captures on this machine (ser9) lands in this
folder. This file is installed from `~/meet-recorder/RECORDINGS.md`.

## Layout

```
~/Recordings/
└── YYYY-MM-DD/                         one folder per day (local time)
    └── HHMMSS-<source>[-<title>]/      one folder per recording, named by start time
        ├── meeting.m4a                 mixed audio: other people (speakers) + your mic
        ├── transcript.md               heading, duration, then the transcript
        ├── me.m4a                      your microphone only
        ├── others.m4a                  the call audio only (everyone else)
        ├── transcript.txt              transcript only, one line per utterance:
        │                               [mm:ss -> mm:ss] Me: text
        │                               [mm:ss -> mm:ss] Others: text
        ├── transcript.vtt              Teams-style WebVTT transcript (<v Speaker>text</v>);
        │                               drop it into FocusTime (Calls → drop zone)
        ├── segments.json               every utterance with exact float start/end,
        │                               speaker ("Me", "Others" or "") and text
        ├── meta.json                   start time, source, title, devices, model, status,
        │                               "screens": number of screen frames kept,
        │                               "mic_echo_cancel": true if me.m4a is the
        │                               echo-cancelled mic (far side removed),
        │                               "mic_track_reason": why that mic track
        │                               was chosen (when echo cancellation ran)
        ├── screens/                    JPEGs of the Teams window, one per screen change
        │                               (e.g. a new slide), named by offset: 00123.4s.jpg
        ├── screens.json                [{"t": seconds, "file", "window_title", "w", "h"}]
        ├── moments.md                  one section per frame: image + what was said
        │                               from 30 s before to 15 s after it
        ├── summary.md                  AI summary (if configured); action items and
        │                               decisions end in [mm:ss] and link the screen
        │                               that was showing then
        ├── screens.log                 capture errors (only if something went wrong)
        ├── collect.log                 screen-copy errors (only if something went wrong)
        └── transcribe.log              transcription log (only useful if something failed)
```

## Retention

`meet-recorder prune --apply` (weekly timer) archives every meeting
folder that is 45+ days old: the text artifacts (`transcript.*`,
`segments.json`, `meta.json`, `screens.json`, `moments.md`,
`summary.md`) are tar'd into `~/Recordings/.archive/<date>_<folder>.tar.gz`
and the folder -- audio and screens included -- is deleted only after
that archive verifies. The `.archive/` stash is uploaded to the
Meetings Archive Google Drive folder from the VPS and dropped locally
once Drive confirms the upload. Date folders are removed once empty;
anything that isn't a positively identified meeting folder is kept.
Run `meet-recorder prune` without `--apply` to see the plan first.

Screen frames are only kept when a large part of the window (one connected
area of at least a quarter of it) changes and then holds still for a few
seconds (slides, shared documents). Small scattered changes -- speaking
rings, name labels, tiles moving around in the gallery -- are not kept.
Areas that keep moving (live camera tiles beside a shared slide) are
ignored, so a slide is still captured while people are on camera; a window
that is almost all live video produces no frames, and neither does a Teams
window narrower than 900 px (a share in a narrow tile is unreadable). The `screens/`, `screens.json` and `moments.md` entries
are absent when nothing was kept. Screenshots are private to your user
(`screens/` is 0700, the images 0600). They are copied in by a separate job
right after the recording is saved, so they can appear a few seconds after
the audio.

- `<source>` is `teams` (started automatically when a Teams call began) or
  `manual` (started from the bar icon or Super+Shift+R).
- `<title>` is the Teams meeting window title when one could be read,
  e.g. `101502-teams-weekly-ops-sync`.
- Speaker labels: `Me` is whatever your microphone picked up, `Others` is the
  call audio. Individual participants are not told apart or named. When the
  call plays through speakers the mic also hears the far side; that echo is
  filtered out of the transcript. Echo cancellation (`[audio] mic_source =
  auto`, off by default) records an extra echo-cancelled mic track that is
  used only when it proved complete (`mic_echo_cancel: true` in meta.json;
  otherwise `mic_track_reason` says why the raw mic was used).
  `audio_unverified` in meta.json means an encoded file came out short; the
  original WAVs are then kept in the folder named by `wavs_kept_in`.
- Folders from before 2026-11-23 12:00 are named `HHMMSS-meeting`, have no `meta.json`,
  and their transcripts have no speaker labels.

## Finding things

- Latest recording: `cat ~/.local/state/meet-recorder/last.json` (folder + transcript status).
- All transcripts for a day: `ls ~/Recordings/2026-11-23/*/transcript.md`
- Search every transcript: `grep -ril "budget" ~/Recordings --include=transcript.txt`
- `meta.json` → `transcript_status` is `pending`, `done`, `no-speech` or `failed`.
  Re-run a transcript with `meet-recorder transcribe <folder>`.
- Older recordings without `transcript.vtt`: `meet-recorder export <folder>...`
  builds it (and `segments.json`) from the existing transcript.
  The VTT speaker names come from `[speakers] me / others` in config.ini.
  For folders without `meta.json`, a line starting `Me: ` / `Others: ` is
  taken as that speaker (best guess); with `meta.json`, only recordings
  marked `speaker_labels` are treated as labelled.
- Rebuild `moments.md` with `meet-recorder moments <folder>`; re-link screens
  in a summary with `meet-recorder link-screens <folder>` (safe to repeat).
- If `transcript.vtt` is missing after a transcription, the FocusTime export
  failed (you get a notification; transcript.md is fine). Re-run
  `meet-recorder export <folder>`.

## How recording works

- A background service (`systemctl --user status meet-recorder-watch`) watches
  for Microsoft Teams (teams-for-linux) opening the microphone, which means a
  call is live. It starts recording a few seconds in and stops about 20 seconds
  after the call ends. Turn this on/off from the bar icon or with
  `meet-recorder auto on|off`.
- Manual control: click the mic icon in the top bar, press Super+Shift+R, or
  run `meet-recorder start|stop|toggle|status`.
- Transcription runs locally (faster-whisper) after the recording stops and
  takes roughly 5% of the meeting length. Nothing is uploaded.
- Settings: `~/.config/meet-recorder/config.ini`. Source and notes: `~/meet-recorder/`.
