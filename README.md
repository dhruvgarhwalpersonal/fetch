# Fetch 🎵

> **Download any Spotify track as a high-quality MP3 — no sign-up, no API keys, no limits.**

Fetch is a self-hosted web app that turns a Spotify link into a downloadable MP3 in seconds. Paste any Spotify track URL, click Download, and the file lands in your device with embedded metadata (title, artist, album) and cover art baked right in.

**Live demo:** https://fetch.onrender.com *(your Render URL after deploy)*

---

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Local Development Setup](#local-development-setup)
- [Deploying to Render](#deploying-to-render)
- [Setting Up YouTube Cookies (Bot Detection Fix)](#setting-up-youtube-cookies-bot-detection-fix)
- [GitHub Action — Auto-Update yt-dlp](#github-action--auto-update-yt-dlp)
- [Architecture Deep Dive](#architecture-deep-dive)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)
- [Long-Term Maintenance](#long-term-maintenance)
- [FAQ](#faq)

---

## How It Works

When you paste a Spotify link, Fetch runs four steps entirely on the server:

```
Spotify URL
    │
    ▼
Step 1 — METADATA
Scrape Spotify's embed page for title, artist, album
(3 strategies: __NEXT_DATA__ JSON → JSON-LD → og: tags)
Fallback: Spotify oEmbed API → yt-dlp extractor
    │
    ▼
Step 2 — COVER ART
Deezer free search API → 1000×1000 cover_xl image
Fallback: Spotify oEmbed thumbnail
    │
    ▼
Step 3 — YOUTUBE MATCH
yt-dlp ytsearch: finds the best matching video
(no YouTube API key — uses yt-dlp's built-in search only)
Scored by title similarity + "official audio" signals
    │
    ▼
Step 4 — AUDIO DOWNLOAD via Piped API
Extract YouTube video ID from search result
Piped API (open-source YouTube proxy) returns direct audio stream URL
stream downloaded with requests — bypasses Render datacenter IP block
Fallback: yt-dlp direct download if all Piped instances are down
    │
    ▼
Step 5 — CONVERT & TAG
FFmpeg converts raw audio to MP3 at 192kbps
mutagen embeds ID3 tags (title, artist, album, cover art)
File streams directly to your browser — nothing stored
    │
    ▼
Your MP3 — tagged, with cover art, ready to play
```

No files are kept on the server. Every download is written to a temp directory in `/tmp`, streamed to your browser, then deleted immediately.

---

## Features

- **Zero API keys required** — works out of the box with no Spotify, YouTube, or any other developer account
- **High-quality MP3** — 192kbps audio with full ID3 tags
- **Embedded cover art** — album artwork baked into the MP3 file
- **Dynamic UI theming** — background color extracted from album art using a pixel-quantization algorithm
- **Download history** — last 8 downloads saved in browser localStorage
- **Server health indicator** — live cookie/bot-detection status shown in the header
- **Auto-updating yt-dlp** — GitHub Action bumps yt-dlp every Monday automatically
- **Secure cookie handling** — YouTube cookies stored as a base64 Render secret, never committed to git
- **Android client bypass** — yt-dlp impersonates the YouTube Android app to reduce bot detection

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask, flask-cors |
| Audio search | yt-dlp (ytsearch: only — no YouTube API key) |
| Audio download | Piped API (open-source YouTube proxy, bypasses datacenter IP block) |
| Audio conversion | FFmpeg |
| ID3 tag embedding | mutagen |
| Metadata enrichment | Deezer free API, Spotify embed scrape |
| Frontend | Vanilla HTML/CSS/JS (zero dependencies) |
| Fonts | DM Mono + Syne (Google Fonts) |
| Hosting | Render (free tier, Docker) |
| CI | GitHub Actions |

---

## Project Structure

```
fetch/
├── server.py              # Flask backend — all API routes and logic
├── index.html             # Frontend — single-page app
├── style.css              # All styles — glassmorphism UI
├── Dockerfile             # Docker image — Python 3 slim + FFmpeg
├── requirements.txt       # Python dependencies with pinned versions
├── render.yaml            # Render deployment config
└── .github/
    └── workflows/
        └── update-ytdlp.yml   # Weekly yt-dlp auto-update action
```

---

## Local Development Setup

### Prerequisites

You need the following installed on your machine:

- **Python 3.10+** — [python.org](https://python.org)
- **FFmpeg** — required for audio conversion

**Install FFmpeg:**

- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt-get install ffmpeg`
- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

### Step 1 — Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/fetch.git
cd fetch
```

### Step 2 — Install Python dependencies

```bash
pip install flask flask-cors yt-dlp mutagen requests
```

Or using the requirements file:

```bash
pip install -r requirements.txt
```

### Step 3 — Run the server

```bash
python server.py
```

You should see:

```
🎵  Fetch server v2 starting …
📦  Requires: pip install flask flask-cors yt-dlp mutagen requests
🌐  Open:     http://localhost:8000
```

### Step 4 — Open the app

Go to **http://localhost:8000** in your browser.

### Optional — Local cookies for bot detection bypass

If you're hitting YouTube bot errors locally, you can place a `cookies.txt` file in the same folder as `server.py`. The server picks it up automatically on the next request. See [Setting Up YouTube Cookies](#setting-up-youtube-cookies-bot-detection-fix) for how to export this file.

---

## Deploying to Render

### Step 1 — Push your code to GitHub

Create a new GitHub repository and push all project files to the `main` branch. Make sure these files are present at the root:

```
Dockerfile
server.py
index.html
style.css
requirements.txt
render.yaml
```

And `.github/workflows/update-ytdlp.yml` inside the `.github/workflows/` folder.

> ⚠️ **Never commit `cookies.txt` to a public GitHub repo.** Cookies give access to the Google account they belong to. Use the environment variable method instead — covered in the next section.

### Step 2 — Create a Render account

Go to [render.com](https://render.com) and sign up for free.

### Step 3 — Create a new Web Service

1. Click **New → Web Service**
2. Connect your GitHub account if prompted
3. Select your `fetch` repository
4. Render auto-detects the Dockerfile — you should see **Language: Docker**
5. Set the following:
   - **Name:** `fetch` (or whatever you want — this becomes your URL)
   - **Branch:** `main`
   - **Instance Type:** Free
   - **Root Directory:** leave blank

### Step 4 — Deploy

Click **Deploy Web Service**. The first build takes 3–5 minutes because Docker installs FFmpeg and all Python packages. You can watch the live build logs.

Once the build finishes, your app is live at `https://fetch.onrender.com` (or whatever name you chose).

> **Free tier note:** Render's free tier spins down after 15 minutes of inactivity. The first visit after a period of inactivity takes about 30–60 seconds to start up. This is normal.

---

## Setting Up YouTube Cookies (Bot Detection Fix)

This is the most important setup step for a reliable app. Without cookies, YouTube blocks downloads from Render's datacenter IP. With cookies from a Google account, downloads work almost every time.

**The secure approach:** Store cookies as a base64-encoded Render environment secret — they never touch your git repo and are never visible to anyone viewing your code.

### Part 1 — Create a throwaway Google account

1. Go to [accounts.google.com](https://accounts.google.com) → **Create account**
2. Use a fake name — this account is only for cookies, not for personal use
3. Log into YouTube on this account at [youtube.com](https://youtube.com)
4. Watch one or two videos so the account looks like a real user

> Use a throwaway account, not your personal Google account. This keeps your main account safe and means if YouTube ever flags this account, you just make a new one.

### Part 2 — Export cookies.txt from your browser

**Chrome / Brave / Edge:**

1. Install the extension **"Get cookies.txt LOCALLY"** from the Chrome Web Store (search for it by exact name — it's free)
2. Go to **youtube.com** (make sure you're logged in with the throwaway account)
3. Click the extension icon in your toolbar
4. Click **Export** → saves `cookies.txt` to your Downloads folder

**Firefox:**

1. Install the addon **"cookies.txt"** from addons.mozilla.org
2. Go to **youtube.com**
3. Click the addon icon → **Current Site** → **Export**

**Verify the file:** Open `cookies.txt` in a text editor. It should start with something like:

```
# Netscape HTTP Cookie File
# https://curl.se/docs/http-cookies.html
...
.youtube.com    TRUE    /    FALSE    ...
```

If it looks like that, you're good.

### Part 3 — Convert cookies.txt to base64

Open a terminal and run this command (pointing to wherever your `cookies.txt` file is):

**macOS / Linux:**

```bash
base64 < ~/Downloads/cookies.txt | tr -d '\n'
```

**Windows (PowerShell):**

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\Downloads\cookies.txt"))
```

Copy the entire output — it will be a long string of letters and numbers. Do not add any spaces or line breaks.

### Part 4 — Add the secret to Render

1. Go to your Render dashboard → click on your `fetch` service
2. Click **Environment** in the left sidebar
3. Click **Add Environment Variable**
4. Set:
   - **Key:** `YOUTUBE_COOKIES`
   - **Value:** paste the base64 string you just copied
5. Click **Save Changes**

Render will automatically redeploy with the new environment variable. Wait about 2 minutes for the redeploy to finish.

### Part 5 — Verify it worked

Once the redeploy finishes, open your app. The header should show:

```
online · cookies: env-var
```

If it says `no cookies`, the env var didn't load correctly. Double-check that you copied the full base64 string without any line breaks.

### When cookies expire (every 1–2 years)

1. Log back into YouTube with the same throwaway Google account in your browser
2. Re-export `cookies.txt` using the same extension
3. Run the base64 command again
4. Update the `YOUTUBE_COOKIES` environment variable on Render with the new value
5. Render redeploys automatically

Total time: about 5 minutes every 1–2 years.

---

## GitHub Action — Auto-Update yt-dlp

The file `.github/workflows/update-ytdlp.yml` sets up a scheduled job that runs every Monday at 3am UTC. Here is exactly what it does:

1. Checks out your repository
2. Installs yt-dlp and reads the latest version number
3. Updates `requirements.txt` with the new version
4. If the version changed, commits the update and pushes to `main`
5. Render detects the new commit and automatically rebuilds and redeploys

This means your app always runs the freshest yt-dlp without you doing anything.

### Setting it up

The file just needs to exist in the right place in your repo. GitHub Actions picks it up automatically. No extra configuration needed.

**To verify it's working:**

1. Go to your GitHub repo
2. Click the **Actions** tab
3. You should see **"Weekly yt-dlp update"** in the list
4. Click it and then click **Run workflow** to trigger it manually the first time and confirm it works

**Triggering manually:**

You can trigger the Action at any time from the Actions tab → **Run workflow**. Useful if yt-dlp just released a fix for a YouTube issue and you don't want to wait until Monday.

### Required GitHub permission

The Action needs write permission to push commits. This is enabled by default for Actions in your own repos. If you see a permission error, go to **Settings → Actions → General → Workflow permissions** and set it to **Read and write permissions**.

---

## Architecture Deep Dive

### Metadata Pipeline (server.py)

The server tries to get song metadata in this exact order, stopping at the first success:

**Layer 0 — Spotify embed page scrape (`_meta_from_spotify_embed_scrape`)**

Fetches `https://open.spotify.com/embed/track/{id}` and tries three sub-strategies:

1. `__NEXT_DATA__` JSON blob — Spotify's Next.js app embeds the full track entity as structured JSON. This has proper `artists[]` arrays and `album.images[]` sorted by size. Most reliable.
2. JSON-LD `<script type="application/ld+json">` — structured data with `byArtist` array. Second most reliable.
3. `og:` meta tags — `og:description` usually contains "Song · Artist · Album". Last resort scrape.

**Layer 1 — Spotify oEmbed (`_meta_from_spotify_oembed`)**

Calls `https://open.spotify.com/oembed?url=...` — an official public endpoint that requires no auth. Returns a combined "Song · Artist" string. Used both as a metadata fallback and as a cover art source.

**Layer 2 — yt-dlp Spotify extractor (`_meta_from_ytdlp_spotify`)**

yt-dlp has a built-in Spotify extractor. Used as a last resort — sometimes fails on Render due to Spotify bot detection.

**Cover art waterfall:**

```
embed page images[] (highest res, direct from Spotify CDN)
    ↓ if missing
oEmbed thumbnail_url
    ↓ if missing
Deezer cover_xl (1000×1000)
```

**Deezer enrichment (`_meta_from_deezer`):**

After any successful metadata layer, the server queries Deezer's free search API with the title + artist. This gives a 1000×1000 cover image (`cover_xl`) and confirms/corrects the album name. Deezer results are scored by title + artist similarity so the correct track is always selected from search results.

### YouTube Search (server.py)

`_search_youtube` calls `ytsearch5:{title} {artist} official audio` via yt-dlp's built-in search. No YouTube Data API key is used — yt-dlp scrapes the search results directly.

The top 5 results are scored by:
- Title similarity to the known song name
- Channel name similarity to the artist name
- Presence of "official" or "audio" in the video title
- Exact artist name match in the channel name (biggest score boost)

The highest-scoring result's URL is returned.

### Download & Streaming (server.py)

`/api/stream-download` does the following:

1. Creates a unique temp directory in `/tmp` (ephemeral on Render, writable unlike the app directory)
2. Extracts the YouTube video ID from the URL returned by the search step
3. **Piped API path (primary):** Queries each Piped instance in order via `GET /streams/<video_id>`. Piped is an open-source YouTube frontend whose community-run instances are not on YouTube's datacenter blocklist — meaning audio streams actually deliver from Render's IP. The `audioStreams` array is parsed and the highest-quality m4a or webm stream is selected. The stream is downloaded in 64KB chunks via `requests`. FFmpeg is then called via `subprocess` to convert the raw audio to MP3 at 192kbps.
4. **yt-dlp fallback (if all Piped instances fail):** Falls back to the original yt-dlp `bestaudio/best` + `FFmpegExtractAudio` postprocessor path. This preserves full functionality even during Piped outages.
5. Once the MP3 exists in `/tmp`, streams it to the browser in 64KB chunks using Flask's `stream_with_context`
6. Deletes the temp directory in a `finally` block whether or not streaming succeeded

MP3 files are never stored permanently — `/tmp` is wiped on Render service restarts.

### Cookie System (server.py)

`_best_cookie_source()` returns a yt-dlp options dict in priority order:

1. `YOUTUBE_COOKIES` env var → decoded from base64 → written to a temp file once per process → `{'cookiefile': path}`
2. `cookies.txt` on disk → `{'cookiefile': COOKIES_FILE}` (local dev)
3. Browser probe → tries each browser until one works → `{'cookiesfrombrowser': (browser,)}` (local dev)
4. Empty dict → no cookies, Android client args still active

### Frontend (index.html)

Pure vanilla JavaScript — zero libraries, zero build step.

The color extraction algorithm samples the album art into a 50×50 canvas, buckets pixels into 32-step quantized colors, scores buckets by `frequency × saturation`, picks the most vivid dominant color, and applies it to CSS variables that drive the background, button color, glow effects, and progress bar — all updating in real time when a track loads.

Download history is stored in `localStorage` under the key `fetch_h` (last 8 tracks, serialized as JSON).

---

## API Reference

All endpoints are on the same server that serves the HTML.

### `GET /api/ping`

Health check.

**Response:** `{"ok": true}`

---

### `GET /api/cookie-status`

Returns the current cookie authentication status.

**Response:**
```json
{
  "source": "env",       // "env" | "file" | "browser" | "none"
  "ok": true,
  "browser": "env-var"   // or "file", "chrome", etc.
}
```

---

### `GET /api/spotify-meta`

Fetches track metadata. Two modes:

**Mode A — client provides title + artist (faster):**
```
GET /api/spotify-meta?title=Blinding+Lights&artist=The+Weeknd
```

**Mode B — server scrapes from track ID (fallback):**
```
GET /api/spotify-meta?id=0VjIjW4GlUZAMYd2vXMi3b
```

**Response:**
```json
{
  "title": "Blinding Lights",
  "artist": "The Weeknd",
  "album": "After Hours",
  "cover": "https://i.scdn.co/image/...",
  "source": "embed-nextdata+deezer"
}
```

---

### `GET /api/youtube-search`

Finds the best YouTube match for a track.

```
GET /api/youtube-search?title=Blinding+Lights&artist=The+Weeknd
```

**Response:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=4NRXx6U8ABQ"
}
```

---

### `POST /api/stream-download`

Downloads and streams the MP3 to the client.

**Request body:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=4NRXx6U8ABQ",
  "title": "Blinding Lights",
  "artist": "The Weeknd"
}
```

**Response:** Binary MP3 stream with headers:
```
Content-Type: audio/mpeg
Content-Disposition: attachment; filename="Blinding Lights - The Weeknd.mp3"
Content-Length: {bytes}
```

---

## Troubleshooting

### "All Piped instances failed"

This means every public Piped instance in the list was unreachable or returned an error when trying to fetch the audio stream URL. This is uncommon — the six instances in the list are geographically distributed and rarely all down at the same time.

When this happens, Fetch **automatically falls back to yt-dlp** for the download. You do not need to do anything. If yt-dlp also fails with bot detection errors, refer to the section above about `YOUTUBE_COOKIES`.

If you're seeing this consistently, the Piped instance list in `server.py` (the `PIPED_INSTANCES` constant near the top of the file) can be updated with currently active instances from the [Piped instances list](https://github.com/TeamPiped/Piped/wiki/Instances).

### "YouTube blocked the download (bot detection)"

This is the most common issue on Render. Fix it by setting up the `YOUTUBE_COOKIES` environment variable — see [Setting Up YouTube Cookies](#setting-up-youtube-cookies-bot-detection-fix).

If cookies are already set and you're still seeing this:
- The cookies may have expired — re-export and update the env var
- Try triggering a manual yt-dlp update via the GitHub Actions tab

### The app shows "checking…" and never goes online

The Render free tier sleeps after 15 minutes of inactivity. The first visit triggers a cold start which takes 30–90 seconds. Just wait and refresh.

### Cover art doesn't appear

Deezer's free API occasionally returns no results for obscure tracks. The app will still download — it just won't have cover art in the UI (the file will also not have embedded cover art in this case).

### Download completes but the MP3 file is 0 bytes or silent

This was a bug in older versions caused by FFmpeg being unable to pipe to stdout. It is fixed — the current version writes to `/tmp` first then streams the finished file. Make sure you're running the latest `server.py`.

### Metadata shows wrong song or artist

The Spotify embed scrape occasionally picks up a wrong value (rare). The Deezer similarity scoring usually corrects it. If a track consistently shows wrong metadata, it may be a very obscure track not in Deezer's database.

### "Could not extract metadata from Spotify page"

Spotify may have temporarily bot-blocked the server's IP for the embed scrape. Wait a few minutes and try again. This is rare and self-resolving.

### yt-dlp errors on first boot

If you see yt-dlp errors immediately after deploying, the server's auto-update thread may not have finished yet. Wait 60 seconds and try again — the auto-update runs in the background at startup.

---

## Long-Term Maintenance

Here is an honest breakdown of what will and won't require attention over time:

### Automatic (zero effort from you)

- **yt-dlp version** — GitHub Action updates it every Monday. Render rebuilds automatically.
- **Python version** — Dockerfile uses `python:3-slim` (not a pinned version), so it always pulls the latest stable Python on each build.
- **yt-dlp startup update** — server runs `pip install --upgrade yt-dlp` in a background thread on every boot, as an extra safety net between Monday rebuilds.

### Periodic (every 1–2 years)

- **YouTube cookies** — re-export `cookies.txt` from your browser, re-encode to base64, update the Render env var. Takes about 5 minutes. You'll know it's time when downloads start failing with bot detection errors despite cookies being set.

### Occasional (when Spotify redesigns)

- **Spotify embed scraper** — Spotify redesigns their web app every 1–3 years. When they do, the `__NEXT_DATA__` path or og: tag format may change. The fix is updating the scrape logic in `_meta_from_spotify_embed_scrape`. Spotify's oEmbed API fallback is more stable and may carry things until the scraper is fixed.

### Never breaks

- **Frontend** — pure vanilla JS with no dependencies
- **Deezer API** — no auth, very stable
- **Flask, mutagen, requests** — extremely stable libraries with years of runway

---

## FAQ

**Is this legal?**

This is for personal use. Downloading copyrighted music for personal listening falls into a legal grey area that varies by country. Do not use this to distribute music or commercially exploit downloaded tracks.

**Does this work for Spotify playlists or albums?**

Currently only individual track URLs are supported. The URL must match the pattern `https://open.spotify.com/track/{id}`.

**Why does the app use YouTube and not Spotify's audio directly?**

Spotify does not provide a public audio download API. The tracks live behind DRM. YouTube is used as the audio source because it has the same songs in high quality and yt-dlp can download from it. The Spotify link is only used to look up the song's metadata.

**Why is the audio quality 192kbps and not 320kbps?**

The streaming download route uses 192kbps for faster streaming. The older job-based download route (still in the code but not used by the main UI) uses 320kbps. If you need 320kbps, you can change `'preferredquality': '192'` to `'preferredquality': '320'` in the `stream_download` function in `server.py`.

**Can multiple people use this at the same time?**

Yes, each download runs in its own temp directory. Flask handles concurrent requests. On Render's free tier the single instance handles multiple users fine for light traffic.

**The app works locally but not on Render — why?**

Almost always this is the YouTube bot detection issue. Render's IP addresses are known datacenter IPs and YouTube blocks them. The cookies fix resolves this completely.

**How do I update yt-dlp manually without waiting for Monday?**

Go to your GitHub repo → **Actions** tab → **Weekly yt-dlp update** → **Run workflow**. This triggers an immediate update and redeploy.

---

## Contributing

Pull requests are welcome. For major changes, open an issue first.

---

## License

MIT — do whatever you want with this code, just don't hold the author liable.
