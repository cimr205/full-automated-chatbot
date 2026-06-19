# MT5 agent — runs MT5 under Wine + mt5linux so it can live on Railway
# (no separate VPS, no Windows PC, no manual login).
#
# Heavy image: Wine + a Windows Python + the MT5 terminal. Build can take
# 10-15 minutes the first time — that's normal.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEARCH=win64
ENV WINEDEBUG=-all
ENV DISPLAY=:99

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      wine64 wine32 winetricks xvfb wget ca-certificates cabextract unzip \
      python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Visual C++ runtime — the MT5 terminal needs this to launch under Wine
RUN xvfb-run -a winetricks -q vcrun2019 corefonts

# Windows Python (inside Wine) — MetaTrader5 pip package only works on Windows Python
RUN wget -q -O /tmp/python-installer.exe \
      https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe \
 && xvfb-run -a wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 \
 && rm /tmp/python-installer.exe

RUN xvfb-run -a wine python -m pip install --no-cache-dir MetaTrader5

# MT5 terminal itself, silent install to the default path
RUN wget -q -O /tmp/mt5setup.exe \
      https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe \
 && xvfb-run -a wine /tmp/mt5setup.exe /auto \
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
