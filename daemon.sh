#!/bin/sh
# This script runs exhale.py in an interactive screen daemon.
# Usage: /home/pi/daemon.sh [args]

dir="$(dirname -- "$(realpath -- "$0")")"
h="$(mktemp --tmpdir exhale.XXXX)"
screen -ls exhale && echo "Daemon already running" && exit 1
echo "cd $dir; ./exhale.py run $@" >"$h"

# Beware that screen's "copy mode" will block the daemon:
# https://savannah.gnu.org/bugs/?63341
HISTFILE="$h" screen -dmS exhale -h 10000 bash --init-file "$h" \
    && echo "Daemon started"
