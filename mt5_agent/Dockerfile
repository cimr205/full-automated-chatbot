# MT5 agent — runs MT5 under Wine + mt5linux so it can live on Railway
# (no separate VPS, no Windows PC, no manual login).
#
# Heavy image: Wine + a Windows Python + the MT5 terminal. Build can take
# 10-15 minutes the first time — that's normal.
# Pinned to amd64 — wine64/wine32 (and MT5 itself) need x86_64; without this
# the build can silently resolve to an arm64 base on ARM builders, and wine
# fails with "Exec format error" at runtime.
FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEARCH=win64
ENV WINEDEBUG=-all
ENV DISPLAY=:99
# Disables Wine's built-in Mono/Gecko install prompts — those are GUI dialogs
# that hang forever with no display interaction in a headless build.
ENV WINEDLLOVERRIDES="mscoree=d;mshtml=d"

# Ubuntu's own wine64/wine32 packages are frequently broken in containers
# ("Exec format error" even on a correctly-pinned amd64 image) — install
# from WineHQ's official repo instead, which is what they actually test.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb wget ca-certificates cabextract unzip gnupg \
      python3 python3-pip \
 && mkdir -p /etc/apt/keyrings \
 && wget -q -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
 && wget -q -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources \
 && apt-get update \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Initialize the Wine prefix on its own first — if this is going to hang on
# a Mono/Gecko prompt, fail fast here (within the timeout) instead of inside
# winetricks where it's harder to diagnose.
# `wineserver -w` waits for Wine's background init processes to actually
# finish writing the prefix (registry files etc.) before xvfb-run tears down
# the X display — without it, the prefix is left half-initialized.
RUN xvfb-run -a sh -c "timeout 120 wineboot --init; wineserver -w; exit 0"

# Visual C++ runtime — the MT5 terminal needs this to launch under Wine.
# Installed directly from Microsoft instead of via winetricks: winetricks'
# own internal sanity check (resolving %AppData% through wine cmd.exe) fails
# in this environment and aborts before it even attempts the install, even
# though the Wine prefix itself is fully initialized. The redistributable's
# own /quiet /install flags don't need that check at all.
RUN wget -q -O /tmp/vc_redist.x64.exe \
      https://aka.ms/vs/16/release/vc_redist.x64.exe \
 && xvfb-run -a sh -c "timeout 180 wine /tmp/vc_redist.x64.exe /quiet /install /norestart; rc=\$?; wineserver -w; exit \$rc" \
 && rm /tmp/vc_redist.x64.exe

# Windows Python (inside Wine) — MetaTrader5 pip package only works on Windows Python
RUN wget -q -O /tmp/python-installer.exe \
      https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe \
 && xvfb-run -a sh -c "timeout 180 wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1; rc=\$?; wineserver -w; exit \$rc" \
 && rm /tmp/python-installer.exe

RUN xvfb-run -a sh -c "timeout 180 wine python -m pip install --no-cache-dir MetaTrader5; rc=\$?; wineserver -w; exit \$rc"

# MT5 terminal itself, silent install to the default path
RUN wget -q -O /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe \
 && xvfb-run -a sh -c "timeout 180 wine /tmp/mt5setup.exe /auto; rc=\$?; wineserver -w; exit \$rc" \
 && rm /tmp/mt5setup.exe

# Native Linux side — this is what mt5_worker.py actually runs as
RUN pip3 install --no-cache-dir mt5linux "redis[asyncio]==5.0.4" python-dotenv==1.0.1

COPY mt5_worker.py start.sh ./
RUN chmod +x start.sh

ENV MT5_BACKEND=linux
ENV MT5_LINUX_HOST=localhost
ENV MT5_LINUX_PORT=18812
ENV MT5_TERMINAL_PATH="C:\\Program Files\\MetaTrader 5\\terminal64.exe"
ENV PYTHONUNBUFFERED=1

CMD ["./start.sh"]
