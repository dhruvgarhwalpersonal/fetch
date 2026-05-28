FROM python:3-slim

# Install FFmpeg + Node.js (Node is used by bgutil po_token helper)
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs npm && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Anti-bot: optional browser cookie source ──────────────────────────────────
# Set at runtime via Render env var dashboard: YTDLP_COOKIES_BROWSER=chrome
# Leave unset (default) — tv_embedded + auto po_token handles it.
ENV YTDLP_COOKIES_BROWSER=""

# po_token is generated automatically on startup — no env var needed.

EXPOSE 8000
CMD ["python", "server.py"]
