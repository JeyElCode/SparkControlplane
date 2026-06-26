#!/bin/sh
# Ensure the data dir is writable by the unprivileged user, then drop privileges.
set -e

DATA_DIR="${SPARK_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
  chown -R spark:spark "$DATA_DIR" 2>/dev/null || true
  exec gosu spark "$@"
fi

exec "$@"
