# Gratis VPS til MT5 (uden at betale for en server)

Dette er metoden hvis du ikke har en Windows-PC der kan stå tændt. Vi bruger
**Oracle Cloud "Always Free"** — en virtuel maskine der er $0/måned for
evigt (ikke en 30-dages prøveperiode), kører **MT5 under Wine** på den, og
forbinder via `mt5linux` (en lille bro der lader Python tale med en
MT5-terminal, der kører i Wine, som om den var native).

**Vær ærlig med dig selv om det her:** Det er gratis, men det er ikke så
stabilt som en rigtig Windows-VPS. Wine + MT5 kan i sjældne tilfælde
kræve genstart. Hvis du på et tidspunkt vil have noget der bare virker uden
opsyn, er en billig Windows-VPS (~50-100 kr/md) det robuste alternativ —
men denne guide er $0.

## 1. Opret en gratis Oracle Cloud-konto

1. Gå til oracle.com/cloud/free og opret en konto (kræver et kort til
   identitetsbekræftelse — du bliver **ikke** opkrævet for Always Free-ressourcer).
2. Under "Create a VM instance": vælg **shape** = `VM.Standard.A1.Flex`
   (Ampere/ARM — Always Free giver dig op til 4 OCPU + 24 GB RAM helt gratis).
   Hvis A1 ikke er tilgængelig i din region, brug `VM.Standard.E2.1.Micro` (mindre, men også gratis).
3. Vælg image: **Ubuntu 22.04**.
4. Gem din SSH-nøgle (Oracle viser dig den til download) — du skal bruge den til at logge ind.
5. Åbn port **22 (SSH)** i "Security List" / VCN — det er normalt åbent som standard.

## 2. SSH ind og installer Wine

```bash
ssh -i din-nøgle.key ubuntu@DIN_VM_IP

sudo dpkg --add-architecture i386
sudo apt update
sudo apt install -y wine64 wine32 winetricks xvfb python3-pip python3-venv

# Virtuel skærm — MT5's vindue skal kunne "tegnes" selv uden monitor
sudo apt install -y x11vnc   # valgfrit: lader dig se MT5 via VNC for at logge ind
```

## 3. Installer MT5 i Wine

```bash
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x16 &

# Download MT5-installeren fra din broker (eller metatrader5.com)
wget -O mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"
wine mt5setup.exe
```

Følg installations-guiden i Wine (den popper op via Xvfb — brug `x11vnc` +
en VNC-klient på din egen computer for at se og klikke gennem den, da
serveren ikke har en rigtig skærm).

**Vigtigt:** Når MT5 er installeret, åbn det og log ind med din
broker-konto (demo først!) **mindst én gang manuelt** via VNC, så MT5
husker login og kan starte automatisk bagefter.

## 4. Installer mt5linux

`mt5linux` kører en lille Python-server *inde i Wine* (hvor selve
MetaTrader5-pakken bor) og eksponerer den til normal Linux-Python via RPyC.

```bash
# Find Wine's python.exe-sti (eller installer Python i Wine hvis det ikke er der)
wine python -m pip install MetaTrader5 mt5linux

# Native Linux-side (det din mt5_worker.py rent faktisk kører i):
python3 -m venv ~/mt5env
source ~/mt5env/bin/activate
pip install mt5linux redis[asyncio] python-dotenv
```

Start broen (peg på Wine's python.exe — find stien med `find ~/.wine -name python.exe`):

```bash
python3 -m mt5linux --host 0.0.0.0 -p 18812 "/home/ubuntu/.wine/drive_c/Program Files/Python311/python.exe" &
```

## 5. Konfigurer og start mt5_worker.py

Kopier `mt5_agent/` til VM'en, opret `.env` med:

```
REDIS_URL=<din Railway Redis URL>
MT5_BACKEND=linux
MT5_LINUX_HOST=localhost
MT5_LINUX_PORT=18812
```

```bash
source ~/mt5env/bin/activate
export DISPLAY=:99
python3 mt5_worker.py
```

Du bør se `mt5linux-bro fundet ✓` i loggen, og din Telegram-bot sender
`✅ MT5 Worker tilkoblet`.

## 6. Få det til at overleve genstart (systemd)

```bash
sudo tee /etc/systemd/system/mt5-xvfb.service <<'EOF'
[Unit]
Description=Virtual display for MT5
[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1024x768x16
Restart=always
[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/mt5-linux-bridge.service <<'EOF'
[Unit]
Description=mt5linux bridge (Wine MT5)
After=mt5-xvfb.service
[Service]
Environment=DISPLAY=:99
ExecStart=/usr/bin/python3 -m mt5linux --host 0.0.0.0 -p 18812 "/home/ubuntu/.wine/drive_c/Program Files/Python311/python.exe"
Restart=always
[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/mt5-worker.service <<'EOF'
[Unit]
Description=Trading bot MT5 worker
After=mt5-linux-bridge.service
[Service]
WorkingDirectory=/home/ubuntu/mt5_agent
Environment=DISPLAY=:99
ExecStart=/home/ubuntu/mt5env/bin/python3 mt5_worker.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now mt5-xvfb mt5-linux-bridge mt5-worker
```

Nu kører alt automatisk igen efter en genstart af VM'en — helt gratis.

## Fejlfinding

- **MT5 logger ud af sig selv:** log ind manuelt via VNC igen, og tjek at
  "Algo Trading" er aktiveret i MT5 (knappen i værktøjsbjælken).
- **`mt5linux-bro ikke tilgængelig`:** tjek at broen kører
  (`sudo systemctl status mt5-linux-bridge`) og at porten matcher `.env`.
- **Wine crasher under høj belastning:** Always Free-VM'er har begrænset
  CPU — overvej at skrue `MONITOR_INTERVAL` op (færre scans i timen) hvis
  det sker ofte.
