FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc wget curl \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libpangocairo-1.0-0 libxshmfence1 libx11-6 libxcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy Ollama binary from official image (no install script needed)
COPY --from=ollama/ollama:latest /bin/ollama /usr/local/bin/ollama

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .
RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV BROWSER_DATA_DIR=/tmp/browser-data
# Ollama runs as sidecar on localhost — no external service needed
ENV OLLAMA_URL=http://localhost:11434
ENV OLLAMA_MODEL=llama3.2:1b

EXPOSE 8000
CMD ["./start.sh"]
