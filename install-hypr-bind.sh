#!/usr/bin/env bash
# Add the "Meeting snap" Hyprland binding next to the recorder toggle in
# ~/.config/hypr/bindings.lua. Idempotent: does nothing if a
# `meet-recorder snap` binding is already there. Picks SUPER + ALT + R if
# `hyprctl binds -j` shows it free, else SUPER + SHIFT + U. Reloads Hyprland
# only if it changed the file; if `hyprctl configerrors` is then not empty,
# the file is restored, Hyprland reloaded again and a warning printed.
#
# Usage: install-hypr-bind.sh [bindings.lua]   (HYPRCTL=... for tests)
set -euo pipefail

file=${1:-$HOME/.config/hypr/bindings.lua}
hyprctl=${HYPRCTL:-hyprctl}
anchor='o.bind("SUPER + SHIFT + R", "Meeting recorder", "meet-recorder toggle")'

if [ ! -f "$file" ]; then
    echo "warning: $file not found; snap hotkey not installed" >&2
    exit 0
fi
if grep -q 'meet-recorder snap' "$file"; then
    echo "snap hotkey: already in $file"
    exit 0
fi
if ! grep -qF "$anchor" "$file"; then
    echo "warning: recorder toggle binding not found in $file; snap hotkey not installed" >&2
    exit 0
fi

# Which combos are taken? modmask bits: SHIFT 1, CTRL 4, ALT 8, SUPER 64.
binds=$("$hyprctl" binds -j 2>/dev/null || echo '[]')
combo=$(printf '%s' "$binds" | python3 -I -c '
import json, sys
try:
    taken = {(b.get("modmask"), str(b.get("key", "")).upper())
             for b in json.load(sys.stdin)}
except ValueError:
    taken = set()
for mods, key, name in ((72, "R", "SUPER + ALT + R"),
                        (65, "U", "SUPER + SHIFT + U")):
    if (mods, key) not in taken:
        print(name)
        break
')
if [ -z "$combo" ]; then
    echo "warning: SUPER+ALT+R and SUPER+SHIFT+U are both taken; snap hotkey not installed" >&2
    exit 0
fi

line="o.bind(\"$combo\", \"Meeting snap\", \"meet-recorder snap\")"
backup=$(mktemp "${file}.meet-recorder.XXXXXX")
cp -p "$file" "$backup"
python3 -I - "$file" "$anchor" "$line" <<'PY'
import os, sys
path, anchor, line = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines(keepends=True)
out = []
for l in lines:
    out.append(l)
    if l.strip() == anchor:
        indent = l[:len(l) - len(l.lstrip())]
        out.append(indent + line + "\n")
tmp = path + ".tmp-meet-recorder"
with open(tmp, "w", encoding="utf-8") as f:
    f.writelines(out)
os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)
PY

"$hyprctl" reload >/dev/null 2>&1 || true
errors=$("$hyprctl" configerrors 2>&1 || true)
if [ -n "$(printf '%s' "$errors" | tr -d '[:space:]')" ]; then
    cp -p "$backup" "$file"
    rm -f "$backup"
    "$hyprctl" reload >/dev/null 2>&1 || true
    echo "warning: Hyprland reported config errors after adding the snap hotkey; reverted:" >&2
    printf '%s\n' "$errors" >&2
    exit 0
fi
rm -f "$backup"
echo "snap hotkey: $combo -> meet-recorder snap (added to $file)"
