#!/usr/bin/env bash
# Sync the workspace to the installed locations on SER9.
# Run this after editing — it refuses to sync if the tests don't pass.
set -euo pipefail
cd "$(dirname "$0")"

make check

plugin=~/.config/omarchy/plugins/jennings.meetrecorder
unit=~/.config/systemd/user/meet-recorder-watch.service

install -m 755 meet-recorder        ~/.local/bin/meet-recorder.py
install -m 755 meet-recorder-wrapper ~/.local/bin/meet-recorder
install -m 755 meet-recorder-summarize ~/.local/bin/meet-recorder-summarize
install -d "$plugin"
install -m 644 omarchy-widget/Panel.qml      "$plugin/Panel.qml"
install -m 644 omarchy-widget/manifest.json  "$plugin/manifest.json"
rm -f "$plugin/BarWidget.qml"   # replaced by Panel.qml in 2.0
install -D -m 644 systemd/meet-recorder-watch.service "$unit"
install -d ~/Recordings
install -m 644 RECORDINGS.md ~/Recordings/README.md
# Echo cancellation runs in a per-recording process now (meet-recorder
# starts `pipewire -c <session>/aec.conf`); the daemon must never load it.
# Remove the drop-in an older version may have installed (config only:
# nothing is restarted, the running daemon is untouched).
rm -f ~/.config/pipewire/pipewire.conf.d/50-meet-recorder-aec.conf

systemctl --user daemon-reload
systemctl --user enable meet-recorder-watch.service
systemctl --user restart meet-recorder-watch.service

# Hot-reload keeps the old compiled QML; a shell restart reliably picks up the widget.
omarchy restart shell >/dev/null 2>&1 || true

echo "synced: CLI + summarizer -> ~/.local/bin, bar widget -> omarchy plugin dir, watch service + shell restarted"
