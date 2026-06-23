# MT5 agent — runs MT5 under Wine + mt5linux so it can live on Railway
# (no separate VPS, no Windows PC, no manual login).
#
# Heavy image, slow first start: Wine + a Windows Python + the MT5 terminal
# are installed at CONTAINER STARTUP (see start.sh), not at build time.
# Build time only installs apt packages — actually running wine/exe files
# during `docker build` hit repeated, unexplained failures ("Exec format
# error", "ShellExecuteEx failed") that don't occur once the container is
# actually running normally, which strongly suggests BuildKit's RUN sandbox
# is more restrictive than a real running container on this platform.
FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEARCH=win64
ENV WINEDEBUG=-all
ENV DISPLAY=:99
# Disables Wine's built-in Mono/Gecko install prompts — those are GUI dialogs
# that hang forever with no display interaction in a headless build.
ENV WINEDLLOVERRIDES="mscoree=d;mshtml=d"
# Put the Wine prefix under /data so it survives container restarts if you
# attach a Railway volume at /data — otherwise the ~10-15 min first-run
# setup (Wine prefix init, VC++ runtime, Windows Python, MT5 terminal)
# repeats on every restart.
ENV WINEPREFIX=/data/wine

# Ubuntu's own wine64/wine32 packages are frequently broken in containers —
# install from WineHQ's official repo instead, which is what they actually
# test. The i386 multiarch dependency (for wine-stable-i386, pulled in by
# the winehq-stable meta-package) is required: wine-stable-amd64 alone
# ships no wine/wineboot/wineserver launcher at all, only DLLs (confirmed
# via dpkg -L) — those commands only exist in the full meta-package.
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
