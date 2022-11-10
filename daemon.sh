#!/bin/sh
dir="$(dirname -- "$(realpath -- "$0")")"
h="$(mktemp --tmpdir exhale.XXXX)"
screen -ls exhale && echo "Daemon already running" && exit 1

# This script runs exhale.py in an interactive screen daemon.
# You can customize these command line flags:
echo "cd $dir; ./exhale.py co2 --zdevice=/dev/ttyS0 --scd30_i2c=6" >"$h"

HISTFILE="$h" screen -dmS exhale -h 10000 bash --init-file "$h" \
    && echo "Daemon started"
