FROM python:3.11-slim

WORKDIR /app

# 🔥 System deps (added for Pillow + reportlab)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates fonts-liberation \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libpangocairo-1.0-0 libasound2 libgtk-3-0 libxshmfence1 \
    libjpeg-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Playwright
RUN python -m playwright install --with-deps chromium

COPY . .

ENV PORT=10000

EXPOSE 10000

# 🔥 Increased timeout for heavy PDFs
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app", "--workers=1", "--threads=2", "--timeout=300"]
