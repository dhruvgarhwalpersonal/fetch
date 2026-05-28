FROM python:3.11-slim

# FFmpeg is all we need — yt-dlp uses it to convert audio to MP3
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects PORT automatically; default to 8000 for local Docker runs
ENV PORT=8000

EXPOSE 8000
CMD ["python", "server.py"]
