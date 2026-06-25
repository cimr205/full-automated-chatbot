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

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 2

# VNC kept as a diagnostic fallback — AutoTrading is now enabled at launch
# via the /config: ini below (see MT5_CONFIG_INI), so manual clicking
# shouldn't be needed. If MT5 still rejects orders with "AutoTrading
# disabled by client", connect via VNC to see what the terminal is actually
# doing. Without a password set, x11vnc runs open — set VNC_PASSWORD in
# Railway and add a TCP proxy (Settings → Networking → TCP Proxy → port
# 5900) to reach it.
if [ -n "${VNC_PASSWORD:-}" ]; then
  x11vnc -display :99 -forever -shared -passwd "$VNC_PASSWORD" -rfbport 5900 &
else
  echo "ADVARSEL: VNC_PASSWORD ikke sat — x11vnc kører uden adgangskode."
  x11vnc -display :99 -forever -shared -nopw -rfbport 5900 &
fi
VNC_PID=$!

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
  timeout 180 wine python -m pip install --no-cache-dir MetaTrader5 mt5linux
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

# mt5linux's server must run INSIDE Wine using Wine's own python (that's
# where MetaTrader5 and mt5linux are installed) — not from the native side.
# Exclude the venv template stub under Lib/venv/scripts/nt/python.exe,
# which `find` would otherwise happily match first.
WINE_PYTHON="$(find "$WINEPREFIX" -iname 'python.exe' -not -path '*/venv/*' 2>/dev/null | head -1)"
if [ -z "$WINE_PYTHON" ]; then
  echo "FEJL: Kunne ikke finde Wine's python.exe efter opsætning."
  exit 1
fi

# Launch the MT5 terminal itself visibly (not just lazily via mt5.initialize()
# inside a trade command) so it's there, logged in, and ready right away.
#
# AutoTrading ("Algo Trading" toggle) is a global flag the terminal reads
# from a /config: ini file at startup — it does NOT need to be clicked by
# hand in the GUI. This is the same mechanism MT5 strategy-tester farms use
# to run headless. [Experts] Enabled=true is the AutoTrading button itself;
# AllowLiveTrading/AllowDllImport are the per-EA permissions an EA would
# otherwise need granted via its own Properties dialog.
MT5_EXE="$(find "$WINEPREFIX" -iname 'terminal64.exe' 2>/dev/null | head -1)"
if [ -n "$MT5_EXE" ]; then
  MT5_CONFIG_INI="/tmp/mt5_startup.ini"
  # MT5 ini files use 1/0, not true/false — "true" is silently ignored and
  # leaves AutoTrading off, which produces error 10027 on every order_send.
  {
    echo "[Common]"
    if [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ] && [ -n "${MT5_SERVER:-}" ]; then
      echo "Login=${MT5_LOGIN}"
      echo "Password=${MT5_PASSWORD}"
      echo "Server=${MT5_SERVER}"
    fi
    echo "AutoConfiguration=false"
    echo ""
    echo "[Experts]"
    echo "AllowLiveTrading=1"
    echo "AllowDllImport=1"
    echo "AllowImport=1"
    echo "Enabled=1"
    echo "Account=ALL"
    echo "Confirm=0"
  } > "$MT5_CONFIG_INI"

  # Use hardcoded Z: path — winepath can fail silently if wineserver isn't
  # fully up yet, which would give MT5 a malformed /config: argument.
  wine "$MT5_EXE" "/config:Z:\\tmp\\mt5_startup.ini" &
  MT5_PID=$!

  # Belt-and-suspenders: after MT5 has had time to create its data directory,
  # patch the persisted terminal.ini directly so AutoTrading survives restarts
  # even if the /config: flag is ignored by this MT5 build/broker combo.
  sleep 20
  TERMINAL_INI=$(find "$WINEPREFIX/drive_c" -name "terminal.ini" 2>/dev/null | head -1)
  if [ -n "$TERMINAL_INI" ]; then
    echo "Patcher terminal.ini for AutoTrading: $TERMINAL_INI"
    if grep -q "^\[Experts\]" "$TERMINAL_INI"; then
      sed -i '/^\[Experts\]/,/^\[/{s/^Enabled=.*/Enabled=1/}' "$TERMINAL_INI"
      grep -q "^Enabled=" "$TERMINAL_INI" || sed -i '/^\[Experts\]/a Enabled=1' "$TERMINAL_INI"
      sed -i '/^\[Experts\]/,/^\[/{s/^AllowLiveTrading=.*/AllowLiveTrading=1/}' "$TERMINAL_INI"
    else
      printf '\n[Experts]\nEnabled=1\nAllowLiveTrading=1\nAllowDllImport=1\nAllowImport=1\n' >> "$TERMINAL_INI"
    fi
  else
    echo "ADVARSEL: terminal.ini ikke fundet endnu — /config: er eneste forsøg."
  fi
fi

wine "$WINE_PYTHON" -m mt5linux --host 0.0.0.0 -p "${MT5_LINUX_PORT:-18812}" &
BRIDGE_PID=$!
sleep 5

python3 mt5_worker.py &
WORKER_PID=$!

wait -n "$XVFB_PID" "$BRIDGE_PID" "$WORKER_PID"
echo "En proces stoppede uventet — lader containeren genstarte."
exit 1
