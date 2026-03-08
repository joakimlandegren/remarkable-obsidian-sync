#!/bin/bash
# Hourly sync wrapper for launchd
# Runs only during daytime (8am–10pm)

HOUR=$(date +%H)
if [ "$HOUR" -lt 8 ] || [ "$HOUR" -ge 22 ]; then
    exit 0
fi

cd "$(dirname "$0")"
uv run python remarkable_to_obsidian.py 2>&1
