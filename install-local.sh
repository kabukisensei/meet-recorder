#!/usr/bin/env bash
# Sync the workspace to the installed locations on SER9.
# Run this after editing — it refuses to sync if the tests don't pass.
set -euo pipefail
cd "$(dirname "$0")"

make check

install -m 755 meet-recorder        ~/.local/bin/meet-recorder.py
install -m 755 meet-recorder-wrapper ~/.local/bin/meet-recorder
install -m 644 omarchy-widget/BarWidget.qml  ~/.config/omarchy/plugins/jennings.meetrecorder/BarWidget.qml
install -m 644 omarchy-widget/manifest.json  ~/.config/omarchy/plugins/jennings.meetrecorder/manifest.json

echo "synced: CLI -> ~/.local/bin, bar widget -> omarchy plugin dir"
