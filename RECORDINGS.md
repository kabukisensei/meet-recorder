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
        ├── meta.json                   start time, source, title, devices, model, status
        └── transcribe.log              transcription log (only useful if something failed)
```

- `<source>` is `teams` (started automatically when a Teams call began) or
  `manual` (started from the bar icon or Super+Shift+R).
- `<title>` is the Teams meeting window title when one could be read,
  e.g. `101502-teams-weekly-ops-sync`.
- Speaker labels: `Me` is whatever your microphone picked up, `Others` is the
  call audio. Individual participants are not told apart or named. When the
  call plays through speakers, the mic's echo of the others is filtered out.
- Folders from before 2026-11-23 12:00 are named `HHMMSS-meeting`, have no `meta.json`,
  and their transcripts have no speaker labels.

## Finding things

- Latest recording: `cat ~/.local/state/meet-recorder/last.json` (folder + transcript status).
- All transcripts for a day: `ls ~/Recordings/2026-11-23/*/transcript.md`
- Search every transcript: `grep -ril "budget" ~/Recordings --include=transcript.txt`
- `meta.json` → `transcript_status` is `pending`, `done`, `no-speech` or `failed`.
  Re-run a transcript with `meet-recorder transcribe <folder>`.

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
