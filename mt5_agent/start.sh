#!/bin/bash
# Entrypoint for the Railway "mt5-agent" service.
#
# First run: installs the Wine prefix, VC++ runtime, Windows Python, and
# the MT5 terminal — all things that repeatedly failed when run during
# `docker build` (Exec format error / ShellExecuteEx failed), but work
# fine here at normal container runtime. Takes 10-15 minutes the first
# time; skipped on every run after, via a marker file in $WINEPREFIX.
# Attach a Railway volume at /data so that marker (and the installed
# Wine prefix) survives restarts — otherwise this repeats every restart.
set -u

WINEPREFIX="${WINEPREFIX:-/data/wine}"
MARKER="$WINEPREFIX/.mt5_setup_done"

Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 2

setup_wine() {
  mkdir -p "$WINEPREFIX"
  echo "=== [1/4] wineboot --init ==="
  timeout 180 wineboot --init
  wineserver -w

  echo "=== [2/4] VC++ runtime ==="
  wget -q -O /tmp/vc_redist.x64.exe https://aka.ms/vs/16/release/vc_redist.x64.exe
  timeout 180 wine /tmp/vc_redist.x64.exe /quiet /install /norestart
  wineserver -w
  rm -f /tmp/vc_redist.x64.exe

  echo "=== [3/4] Windows Python + MetaTrader5 package ==="
  wget -q -O /tmp/python-installer.exe \
    https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe
  timeout 180 wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1
  wineserver -w
  rm -f /tmp/python-installer.exe
  timeout 180 wine python -m pip install --no-cache-dir MetaTrader5
  wineserver -w

  echo "=== [4/4] MT5 terminal ==="
  wget -q -O /tmp/mt5setup.exe \
    https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe
  timeout 180 wine /tmp/mt5setup.exe /auto
  wineserver -w
  rm -f /tmp/mt5setup.exe

  touch "$MARKER"
  echo "=== MT5 setup complete ==="
}

if [ -f "$MARKER" ]; then
  echo "MT5 already set up (found $MARKER) — skipping install."
else
  echo "First run — installing Wine prefix + MT5 (10-15 min)..."
  setup_wine
fi

WINE_PYTHON="$(find "$WINEPREFIX" -iname 'python.exe' 2>/dev/null | head -1)"
if [ -z "$WINE_PYTHON" ]; then
  echo "FEJL: Kunne ikke finde Wine's python.exe efter opsætning."
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
