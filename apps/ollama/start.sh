#!/bin/sh
set -e

MODEL="${OLLAMA_MODEL:-llama3.2:1b}"
echo "=== Ollama service starting — model: $MODEL ==="

# Start ollama server in background
OLLAMA_HOST=0.0.0.0 /bin/ollama serve &
SERVER_PID=$!

# Wait up to 60s for API to respond
echo "Waiting for Ollama API to initialize..."
TRIES=0
until curl -sf http://localhost:11434/api/version > /dev/null 2>&1; do
    sleep 2
    TRIES=$((TRIES + 1))
    if [ $TRIES -ge 30 ]; then
        echo "ERROR: Ollama did not start in time"
        exit 1
    fi
done
echo "Ollama API ready"

# Pull model only if not already cached (Railway volume: /root/.ollama)
if /bin/ollama list 2>/dev/null | grep -q "$(echo "$MODEL" | cut -d: -f1)"; then
    echo "Model $MODEL already cached"
else
    echo "Downloading $MODEL — this may take a few minutes on first start..."
    /bin/ollama pull "$MODEL"
    echo "Model $MODEL downloaded and ready"
fi

echo "=== Ollama ready: model=$MODEL ==="
wait $SERVER_PID
