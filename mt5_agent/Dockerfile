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
      winetricks xvfb wget ca-certificates cabextract unzip gnupg \
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
RUN xvfb-run -a timeout 120 wineboot --init || true

# Visual C++ runtime — the MT5 terminal needs this to launch under Wine.
# --force skips winetricks' bundled sha256 check, which goes stale whenever
# Microsoft rotates the redistributable file out from under the cached hash.
# timeout ensures a hung GUI installer fails the build instead of stalling it.
RUN xvfb-run -a timeout 300 winetricks --force -q vcrun2019 corefonts

# Windows Python (inside Wine) — MetaTrader5 pip package only works on Windows Python
RUN wget -q -O /tmp/python-installer.exe \
      https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe \
 && xvfb-run -a timeout 180 wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 \
 && rm /tmp/python-installer.exe

RUN xvfb-run -a timeout 180 wine python -m pip install --no-cache-dir MetaTrader5

# MT5 terminal itself, silent install to the default path
RUN wget -q -O /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe \
 && xvfb-run -a timeout 180 wine /tmp/mt5setup.exe /auto \
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
