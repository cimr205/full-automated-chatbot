#!/bin/sh
MODEL="${OLLAMA_MODEL:-llama3.2:1b}"
echo "=== AES starting: Ollama($MODEL) + FastAPI ==="

# Start Ollama server in background
OLLAMA_HOST=0.0.0.0 ollama serve &

# Pull model asynchronously — doesn't block the API from starting
{
    sleep 10
    if ! ollama list 2>/dev/null | grep -q "$(echo "$MODEL" | cut -d: -f1)"; then
        echo "Downloading AI model: $MODEL (first start — a few minutes)..."
        ollama pull "$MODEL" && echo "=== Model $MODEL ready — AI is now available ==="
    else
        echo "Model $MODEL already cached — AI ready"
    fi
} &

# Start FastAPI as main process (exec hands over PID 1)
exec uvicorn apps.api.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1
