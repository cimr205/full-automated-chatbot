# MT5 på Railway — ingen VPS, ingen separat konto, ingen manuel login

Dette er den mest "rør-ikke-ved-det" løsning, fordi den bruger den Railway-konto
du allerede har til API'en, Telegram-botten, Redis og Postgres. Ingen ny
cloud-konto, ingen Windows-PC der skal stå tændt.

**Hvordan det virker:** En fjerde Railway-service (`mt5-agent`) bygger et
Docker-image med Wine + MetaTrader 5 + Python indeni. Når containeren
starter, logger den selv ind i MT5 (vi sender kontonummer/adgangskode/server
som miljøvariabler), og din eksisterende `mt5_worker.py` kører i samme
container og lytter på Redis som altid. Hvis noget crasher, genstarter
Railway hele containeren automatisk — du behøver ikke gøre noget.

**Vær ærlig om begrænsningerne:**
- Første build tager 10-15 minutter (Wine + en Windows-Python + MT5-installer
  hentes og installeres under build). Det er normalt, ikke en fejl.
- MT5-terminalen under Wine er en lille smule mindre stabil end på rigtig
  Windows. Hvis den crasher, genstarter Railway containeren — det betyder
  typisk et par minutters udfald, ikke at botten "dør".
- Du skal bruge en Railway-plan med nok RAM til Wine + MT5 (Hobby-planen er
  normalt rigeligt — i samme størrelsesorden som jeres eksisterende
  browser-worker service, som allerede kører Chromium).

## Opsætning (5 minutter af din tid, resten er automatisk)

1. **Tilføj en ny service i dit eksisterende Railway-projekt:**
   - Railway dashboard → dit projekt → **+ New** → **GitHub Repo** → vælg
     `full-automated-chatbot` igen (samme repo, ny service).
   - Under service-indstillinger → **Settings** → **Build**: sæt
     "Dockerfile Path" til `mt5_agent/Dockerfile` (Railway læser ellers
     automatisk `mt5_agent/railway.json`, som allerede peger derhen).

2. **Sæt miljøvariabler på den nye service** (Settings → Variables):
   ```
   REDIS_URL=${{Redis.REDIS_URL}}        # reference til jeres eksisterende Redis-service
   MT5_LOGIN=12345678                    # dit MT5 kontonummer (brug demo først!)
   MT5_PASSWORD=din-adgangskode
   MT5_SERVER=DinBroker-Demo             # serverens navn, fx "ICMarkets-Demo02"
   ```
   `MT5_BACKEND`, `MT5_LINUX_HOST/PORT` og `MT5_TERMINAL_PATH` er allerede
   sat i Dockerfilen — du skal ikke gøre noget med dem.

3. **Deploy.** Railway bygger imaget (10-15 min første gang). Når det er
   oppe, sender din Telegram-bot `✅ MT5 Worker tilkoblet`.

4. **Det er det.** Fremtidige `git push` til `main` redeployer automatisk
   ligesom jeres andre services — ingen manuel indlogning, ingen VNC,
   ingen separat VPS at huske på.

## Fejlfinding

- **Bygger men logger aldrig ind:** dobbelttjek `MT5_SERVER` — det skal
  matche broker-serverens navn *eksakt* som det står i MT5 (Hjælp →
  Om → eller login-skærmen i en almindelig MT5-installation).
- **Container genstarter i loop:** se logs i Railway. Næsten altid enten
  forkert `MT5_SERVER`/`MT5_LOGIN`/`MT5_PASSWORD`, eller broker'en kræver
  2FA (deaktiver 2FA på demo-kontoen, eller brug en investor-adgangskode
  hvis broker'en tillader algoritmisk login uden 2FA).
- **For lidt RAM:** opgrader Railway-planen for denne service specifikt
  hvis bygget lykkes men containeren bliver dræbt (OOM) ved opstart.
