FROM python:3-slim

# Install FFmpeg (required by yt-dlp for audio conversion)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ── Anti-bot: optional browser cookie source (Solution 2) ─────────────────
# Set at runtime: docker run -e YTDLP_COOKIES_BROWSER=chrome ...
# Supported values: chrome, firefox, edge, brave, chromium
# Leave unset (default) to skip — iOS/Android player_client handles most cases.
ENV YTDLP_COOKIES_BROWSER=""

EXPOSE 8000
CMD ["python", "server.py"]
