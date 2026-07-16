#!/bin/sh
set -eu

POT_PORT="${POT_PORT:-4416}"
POT_ENABLED="${POT_ENABLED:-1}"
POT_DIR="${POT_DIR:-/opt/bgutil-ytdlp-pot-provider/server}"

start_pot_server() {
  if [ "$POT_ENABLED" = "0" ] || [ "$POT_ENABLED" = "false" ]; then
    echo "POT provider disabled (POT_ENABLED=$POT_ENABLED)"
    return 0
  fi

  if [ ! -f "$POT_DIR/build/main.js" ]; then
    echo "POT provider build missing at $POT_DIR/build/main.js — continuing without it"
    return 0
  fi

  echo "Starting bgutil POT provider on 127.0.0.1:$POT_PORT"
  node "$POT_DIR/build/main.js" --port "$POT_PORT" >/tmp/pot-provider.log 2>&1 &
  echo $! >/tmp/pot-provider.pid

  # Wait briefly so the first YouTube request can use tokens.
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:${POT_PORT}/ping" >/dev/null 2>&1; then
      echo "POT provider ready"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done

  echo "WARNING: POT provider did not become ready in time; YouTube may still fail"
  echo "--- pot-provider.log ---"
  tail -n 40 /tmp/pot-provider.log || true
}

start_pot_server

exec gunicorn app:app \
  --bind "0.0.0.0:${PORT:-5000}" \
  --timeout 660 \
  --workers 1 \
  --keep-alive 5
