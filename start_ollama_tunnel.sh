#!/bin/bash
# Starts Ollama + tunnel, publishes URL to GitHub so Railway bot finds it automatically.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"

# ── Start Ollama if not running ──────────────────────────────────────────────
if ! curl -sf http://localhost:11434/api/version > /dev/null 2>&1; then
    echo "[ollama] Starting..."
    OLLAMA_HOST=0.0.0.0 ollama serve > /tmp/ollama.log 2>&1 &
    sleep 4
fi
echo "[ollama] Running — $(curl -sf http://localhost:11434/api/version)"

# ── Kill old tunnel ──────────────────────────────────────────────────────────
pkill -f "ssh.localhost.run" 2>/dev/null || true
sleep 1

# ── Start new tunnel ─────────────────────────────────────────────────────────
ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
    -R 80:localhost:11434 ssh.localhost.run > /tmp/lhr.log 2>&1 &
TUNNEL_PID=$!
sleep 7

# ── Extract public URL ────────────────────────────────────────────────────────
TUNNEL_URL=$(grep -o 'https://[a-z0-9]*\.lhr\.life' /tmp/lhr.log | head -1)
if [ -z "$TUNNEL_URL" ]; then
    echo "[tunnel] ERROR: Tunnel URL not found. Check /tmp/lhr.log"
    exit 1
fi
echo "[tunnel] URL: $TUNNEL_URL"

# ── Verify tunnel reaches Ollama ──────────────────────────────────────────────
if ! curl -sf "$TUNNEL_URL/api/version" > /dev/null; then
    echo "[tunnel] ERROR: Tunnel not reachable"
    exit 1
fi
echo "[tunnel] OK — reachable from internet"

# ── Publish URL to GitHub ─────────────────────────────────────────────────────
PASTE_ID="3Orco"  # paste.rs ID — hardcoded in core/ai/chat.py
curl -sf -X PUT --data-binary "$TUNNEL_URL" "https://paste.rs/$PASTE_ID" > /dev/null
echo "[paste.rs] URL updated — Railway bot will find it automatically"
echo ""
echo "=== Klar! Skriv til din Telegram-bot nu ==="
echo "Tunnel: $TUNNEL_URL"
echo "Tryk Ctrl+C for at stoppe"

# ── Keep running ──────────────────────────────────────────────────────────────
wait $TUNNEL_PID
