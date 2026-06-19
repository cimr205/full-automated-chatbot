#!/bin/bash
# Entrypoint for the Railway "mt5-agent" service.
# Starts the virtual display, the mt5linux bridge (which launches MT5 under
# Wine and auto-logs in via MT5_LOGIN/MT5_PASSWORD/MT5_SERVER), then the
# worker itself. If any of the three dies, the whole container exits so
# Railway's restart policy brings everything back up clean.
set -u

Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 2

WINE_PYTHON="$(find /root/.wine -iname 'python.exe' 2>/dev/null | head -1)"
if [ -z "$WINE_PYTHON" ]; then
  echo "FEJL: Kunne ikke finde Wine's python.exe — tjek Dockerfile build."
  exit 1
fi

python3 -m mt5linux --host 0.0.0.0 -p "${MT5_LINUX_PORT:-18812}" "$WINE_PYTHON" &
BRIDGE_PID=$!
sleep 5

python3 mt5_worker.py &
WORKER_PID=$!

wait -n "$XVFB_PID" "$BRIDGE_PID" "$WORKER_PID"
echo "En proces stoppede uventet — lader containeren genstarte."
exit 1
