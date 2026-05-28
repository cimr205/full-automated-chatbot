FROM python:3.11-slim

# System deps: Postgres client + all Chromium/Playwright dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc wget \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libpangocairo-1.0-0 libxshmfence1 libx11-6 libxcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
ENV BROWSER_DATA_DIR=/tmp/browser-data

EXPOSE 8000
CMD ["sh", "-c", "uvicorn apps.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
