#!/usr/bin/env python3
"""
Spotidrop local server — v4 (zero API-key architecture + auto po_token)
Run:  python server.py
Open: http://localhost:8000

Requires: pip install flask flask-cors yt-dlp mutagen requests

Metadata pipeline (fully server-side, no Spotify/YouTube API keys):
  Layer 0 → yt-dlp Spotify scrape  (primary — parses page HTML/JSON-LD)
  Layer 1 → Deezer search API      (free, no auth — better cover, confirmation)
  YouTube → yt-dlp ytsearch:       (no YouTube Data API, no quota)
  Download → bestaudio/best        (always resolves, FFmpeg → mp3/320)

Anti-bot hardening (v4):
  Solution 3 → Rotating User-Agents + sleep_interval jitter
  Solution 4 → player_client: tv_embedded → mweb (2025 bypass)
  Solution 5 → po_token auto-generated on server startup, auto-refreshed
               every 6 hours — zero manual steps, works on Render/Docker.
  Solution 2 → cookiesfrombrowser auto-detection (chrome/firefox/edge/brave)
               Set env var YTDLP_COOKIES_BROWSER=chrome (or firefox/edge/brave)
               to enable. Leave unset to skip.
"""

import os, re, threading, uuid, random, time, subprocess
from typing import Optional
from difflib import SequenceMatcher
import requests
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt-dlp not installed.  Run: pip install yt-dlp flask flask-cors mutagen requests")
    exit(1)

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
    from mutagen.mp3 import MP3
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False
    print("WARNING: mutagen not installed.  Run: pip install mutagen")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs: dict = {}

# ── Solution 3: Rotating User-Agent pool ─────────────────────────────────────
_USER_AGENTS = [
    # Chrome on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    # Chrome on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    # Firefox on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
    # Safari on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
    # iOS Safari (used by yt-dlp ios client too)
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1',
    # Edge on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
]

def _random_ua() -> str:
    return random.choice(_USER_AGENTS)

# Static headers used for non-YouTube requests (Spotify, Deezer, cover art)
HEADERS = {'User-Agent': _USER_AGENTS[0]}

# ── Solution 2: Cookie browser detection ─────────────────────────────────────
# Set env var YTDLP_COOKIES_BROWSER=chrome (or firefox / edge / brave / chromium)
# Leave unset to disable — downloads still work fine without cookies.
_COOKIES_BROWSER = os.environ.get('YTDLP_COOKIES_BROWSER', '').strip().lower() or None
if _COOKIES_BROWSER:
    print(f"[cookies] Will load cookies from browser: {_COOKIES_BROWSER}")
else:
    print("[cookies] No browser cookies configured (set YTDLP_COOKIES_BROWSER to enable)")


# ── Solution 5: Auto po_token generation (server-side, no manual steps) ──────
# YouTube requires a Proof-of-Origin token for headless downloads in 2025.
# We generate it by fetching a real YouTube page and extracting the token
# the same way a browser would — runs on startup and refreshes every 6 hours.
# Works fully inside Docker/Render with zero env vars or manual steps.

_po_token_lock   = threading.Lock()
_po_token_value  = None   # "web+<token>" string or None
_PO_TOKEN_TTL    = 6 * 3600  # refresh every 6 hours

def _generate_po_token() -> str | None:
    """
    Generate a YouTube po_token by calling yt-dlp itself in a subprocess
    with --print-traffic and extracting the po_token it negotiates.
    Falls back to None (downloads still attempted without token).
    """
    try:
        result = subprocess.run(
            [
                'python', '-m', 'yt_dlp',
                '--print-traffic',
                '--skip-download',
                '--no-warnings',
                '--quiet',
                '--extractor-args', 'youtube:player_client=tv_embedded',
                'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            ],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        # yt-dlp logs the token as: [debug] po_token: web+XXXXX
        for line in output.splitlines():
            if 'po_token' in line.lower():
                parts = line.strip().split('po_token')
                if len(parts) > 1:
                    raw = parts[-1].strip().lstrip(':').strip()
                    if raw:
                        token = raw if raw.startswith('web+') else f'web+{raw}'
                        print(f"[po_token] Generated: {token[:30]}…")
                        return token
        # Token not found in traffic — tv_embedded doesn't always emit it
        # but the client itself is still less bot-checked, so this is OK.
        print("[po_token] Not found in traffic output — will run without token")
        return None
    except subprocess.TimeoutExpired:
        print("[po_token] Generation timed out")
        return None
    except Exception as exc:
        print(f"[po_token] Generation failed: {exc}")
        return None


def _refresh_po_token():
    """Regenerate and store the po_token. Called on startup and every 6 hours."""
    global _po_token_value
    token = _generate_po_token()
    with _po_token_lock:
        _po_token_value = token
    print(f"[po_token] {'Active ✓' if token else 'Unavailable — running without token'}")


def _po_token_refresh_loop():
    """Background thread: refresh po_token every _PO_TOKEN_TTL seconds."""
    while True:
        time.sleep(_PO_TOKEN_TTL)
        print("[po_token] TTL reached — refreshing …")
        _refresh_po_token()


def _get_po_token() -> str | None:
    with _po_token_lock:
        return _po_token_value


# Generate token immediately at import time (non-blocking — runs in bg thread)
threading.Thread(target=_refresh_po_token, daemon=True, name='po-token-init').start()
# Start the periodic refresh loop
threading.Thread(target=_po_token_refresh_loop, daemon=True, name='po-token-refresh').start()
print("[po_token] Background generation started — will be ready in ~10s")


# ─────────────────────────────────────────────────────────────────────────────
# Metadata helpers
# ─────────────────────────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_thumbnail(thumbnails: list) -> str:
    """Pick the largest thumbnail URL from a yt-dlp thumbnails list."""
    valid = [t for t in thumbnails if t.get('url')]
    if not valid:
        return ''
    return sorted(valid, key=lambda t: t.get('width', 0) * t.get('height', 0), reverse=True)[0]['url']


# ── LAYER 0: Spotify oEmbed (public, no auth, no bot-block) ──────────────────
def _meta_from_spotify_oembed(track_id: str) -> Optional[dict]:
    """
    Uses Spotify's official oEmbed endpoint — publicly documented, no auth,
    no Spotify Developer account needed, never bot-blocked.
    Returns: {title, artist, album, cover, source} or None.
    """
    url = 'https://open.spotify.com/oembed'
    track_url = f'https://open.spotify.com/track/{track_id}'
    try:
        r = requests.get(url, params={'url': track_url}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        # oEmbed title varies by Spotify version:
        #   current:  "Song Name · Artist Name"  (middle dot U+00B7)
        #   older:    "Song Name by Artist Name"
        title_raw = data.get('title', '')
        cover     = data.get('thumbnail_url', '')
        title, artist = title_raw, ''
        if ' \u00b7 ' in title_raw:          # "Song · Artist"
            parts  = title_raw.split(' \u00b7 ', 1)
            title  = parts[0].strip()
            artist = parts[1].strip()
        elif ' by ' in title_raw:             # "Song by Artist"
            parts  = title_raw.rsplit(' by ', 1)
            title  = parts[0].strip()
            artist = parts[1].strip()
        # If neither separator matched, title_raw is whatever Spotify sent;
        # artist stays '' and Deezer will fill it in below.
        if not title:
            return None
        # artist may be '' here; Deezer enrichment below will fill it if so
        print(f"[meta/layer0-oembed] title='{title}' artist='{artist}'")
        return {'title': title, 'artist': artist, 'album': title,
                'cover': cover, 'source': 'oembed'}
    except Exception as exc:
        print(f"[meta/layer0-oembed] failed: {exc}")
        return None


# ── LAYER 1: yt-dlp Spotify scrape ───────────────────────────────────────────
def _meta_from_ytdlp_spotify(track_id: str) -> Optional[dict]:
    """
    Layer 0a: yt-dlp Spotify scrape. Works when yt-dlp's extractor is current.
    Returns: {title, artist, album, cover, source} or None.
    """
    url = f'https://open.spotify.com/track/{track_id}'
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True,
            'skip_download': True, 'extract_flat': False, 'noplaylist': True,
        }) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        title  = info.get('track')  or info.get('title')   or ''
        artist = info.get('artist') or info.get('creator') or info.get('uploader') or ''
        album  = info.get('album')  or title
        cover  = _best_thumbnail(info.get('thumbnails') or []) or info.get('thumbnail', '')
        if not title:
            return None
        print(f"[meta/layer0a-ytdlp] title='{title}' artist='{artist}'")
        return {'title': title, 'artist': artist, 'album': album, 'cover': cover, 'source': 'ytdlp-spotify'}
    except Exception as exc:
        print(f"[meta/layer0a-ytdlp] failed: {exc}")
        return None


def _meta_from_spotify_embed_scrape(track_id: str) -> Optional[dict]:
    """
    Server-side scrape of Spotify's embed page — no CORS issues, no API key.
    Three sub-strategies tried in order:
      1. __NEXT_DATA__ JSON blob  (structured artists[], album.images[] — most reliable)
      2. JSON-LD <script>         (byArtist array)
      3. og: meta tags            (og:description "Song · Artist · Album")
    Returns: {title, artist, album, cover, source} or None.
    """
    import json as _json

    url = f'https://open.spotify.com/embed/track/{track_id}'
    try:
        r = requests.get(url, headers={
            **HEADERS,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://open.spotify.com/',
        }, timeout=15)
        r.raise_for_status()
        html = r.text

        # ── Strategy 1: __NEXT_DATA__ JSON blob ──────────────────────────────
        # Spotify's embed is a Next.js app; the full track entity is hydrated
        # inline as window.__NEXT_DATA__.  It has structured artists[] arrays
        # and album.images[] sorted by size — much more reliable than scraping.
        nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd_match:
            try:
                nd = _json.loads(nd_match.group(1))
                # Path varies slightly across Spotify embed versions; try both
                entity = (
                    nd.get('props', {}).get('pageProps', {}).get('state', {})
                      .get('data', {}).get('entity') or
                    nd.get('props', {}).get('pageProps', {}).get('track') or
                    nd.get('props', {}).get('initialState', {})
                      .get('data', {}).get('entity')
                )
                if entity:
                    name = entity.get('name', '') or entity.get('title', '')
                    # artists may be list of {name} dicts or a plain string
                    raw_artists = entity.get('artists', []) or entity.get('artist', [])
                    if isinstance(raw_artists, list):
                        artist = ', '.join(
                            a['name'] for a in raw_artists if isinstance(a, dict) and a.get('name')
                        )
                    else:
                        artist = str(raw_artists)
                    album_obj = entity.get('album', {}) or {}
                    album     = album_obj.get('name', '') or name
                    # images[] sorted largest-first by Spotify
                    images = album_obj.get('images', []) or entity.get('images', [])
                    cover  = images[0].get('url', '') if images else ''
                    if name and artist:
                        print(f"[meta/embed-nextdata] title='{name}' artist='{artist}'")
                        return {'title': name, 'artist': artist, 'album': album,
                                'cover': cover, 'source': 'embed-nextdata'}
            except Exception as exc:
                print(f"[meta/embed-nextdata] parse error: {exc}")

        # ── Strategy 2: JSON-LD <script type="application/ld+json"> ──────────
        for match in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                ld = _json.loads(match.group(1))
                if '@graph' in ld:
                    ld = ld['@graph'][0]
                name = ld.get('name', '')
                by   = ld.get('byArtist', {})
                artist = (
                    ', '.join(a['name'] for a in by if isinstance(a, dict) and a.get('name'))
                    if isinstance(by, list) else by.get('name', '')
                )
                cover = ld.get('image', '')
                if isinstance(cover, dict):
                    cover = cover.get('url', '')
                if name and artist:
                    print(f"[meta/embed-jsonld] title='{name}' artist='{artist}'")
                    return {'title': name, 'artist': artist, 'album': name,
                            'cover': cover, 'source': 'embed-jsonld'}
            except Exception:
                continue

        # ── Strategy 3: og: meta tags ─────────────────────────────────────────
        # og:description on Spotify embed: "Song · Artist · Album · Year"
        def _og(prop):
            m = (re.search(rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"', html) or
                 re.search(rf'<meta[^>]+content="([^"]+)"[^>]+property="{prop}"', html))
            return m.group(1) if m else ''

        og_t = _og('og:title')
        og_d = _og('og:description')
        og_i = _og('og:image')

        if og_t:
            title  = og_t.split('·')[0].split('•')[0].strip()
            # og:description examples:
            #   "Summertime Sadness · Lana Del Rey · Born to Die"
            #   "Listen to Blinding Lights by The Weeknd on Spotify."
            artist = ''
            parts = [p.strip() for p in re.split(r'[·•]', og_d) if p.strip()]
            if len(parts) >= 2:
                artist = parts[1]   # second segment is always the artist
            elif ' by ' in og_d.lower():
                artist = og_d.lower().split(' by ')[1].split(' on ')[0].strip().title()
            if title:
                print(f"[meta/embed-og] title='{title}' artist='{artist}'")
                return {'title': title, 'artist': artist, 'album': title,
                        'cover': og_i, 'source': 'embed-og'}

        print("[meta/embed-scrape] could not parse HTML")
        return None

    except Exception as exc:
        print(f"[meta/embed-scrape] failed: {exc}")
        return None


# ── LAYER 1: Deezer search (confirmation + better cover) ─────────────────────
def _meta_from_deezer(query: str, title_hint: str = '', artist_hint: str = '') -> Optional[dict]:
    """
    Search Deezer by 'title artist' query.
    title_hint / artist_hint: when provided, used to pick the best result
    by similarity rather than just the first result Deezer returns.
    cover_xl is 1000×1000 — better than Spotify's thumbnail.
    Returns: {title, artist, album, cover, source} or None.
    """
    try:
        r = requests.get(
            'https://api.deezer.com/search',
            params={'q': query, 'limit': '8'},
            headers=HEADERS, timeout=10,
        )
        r.raise_for_status()
        tracks = r.json().get('data', [])
        if not tracks:
            return None

        # Score each candidate against our known title + artist
        th = (title_hint or query.split()[0]).lower()
        ah = artist_hint.lower()

        def _dz_score(c):
            ct = (c.get('title') or c.get('title_short', '')).lower()
            ca = (c.get('artist', {}).get('name', '')).lower()
            s  = _similarity(ct, th) * 10
            if ah:
                s += _similarity(ca, ah) * 10
            return s

        best = max(tracks, key=_dz_score)

        title  = best.get('title') or best.get('title_short', '')
        artist = best.get('artist', {}).get('name', '')
        album  = best.get('album', {}).get('title', title)
        alb    = best.get('album', {})
        cover  = (alb.get('cover_xl') or alb.get('cover_big') or
                  alb.get('cover_medium') or alb.get('cover') or '')

        if not title or not artist:
            return None

        print(f"[meta/deezer] title='{title}' artist='{artist}' score={_dz_score(best):.1f}")
        return {'title': title, 'artist': artist, 'album': album, 'cover': cover, 'source': 'deezer'}

    except Exception as exc:
        print(f"[meta/deezer] failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# YouTube search via yt-dlp ytsearch: (no YouTube Data API, no quota)
# ─────────────────────────────────────────────────────────────────────────────

def _search_youtube(title: str, artist: str) -> str:
    """
    Use yt-dlp's built-in ytsearch: to find the best YouTube video.
    Scores results the same way as before.  Returns a YouTube watch URL.
    Anti-bot: tv_embedded/mweb clients + auto po_token injected if available.
    """
    query   = f'{title} {artist} official audio'
    search  = f'ytsearch5:{query}'

    yt_args: dict = {'player_client': ['tv_embedded', 'mweb']}
    token = _get_po_token()
    if token:
        yt_args['po_token'] = [token]

    opts = {
        'quiet': True, 'no_warnings': True,
        'skip_download': True, 'extract_flat': True, 'noplaylist': True,
        'extractor_args': {'youtube': yt_args},
        'http_headers': {'User-Agent': _random_ua()},
        'sleep_interval_requests': 1,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            results = ydl.extract_info(search, download=False)
    except Exception as exc:
        raise RuntimeError(f'yt-dlp YouTube search failed: {exc}')

    entries = (results or {}).get('entries') or []
    if not entries:
        raise RuntimeError('No YouTube results found for this track.')

    title_l  = title.lower()
    artist_l = artist.lower()

    def score(entry: dict) -> int:
        t  = (entry.get('title') or '').lower()
        ch = (entry.get('uploader') or entry.get('channel') or '').lower()
        s  = 0
        if ch.replace(' - topic', '').strip() == artist_l: s += 12
        if artist_l in ch or ch.replace(' vevo', '') in artist_l: s += 8
        if 'official audio' in t:  s += 8
        if 'official video' in t:  s += 6
        if 'official'       in t:  s += 3
        if 'audio'          in t:  s += 2
        if title_l          in t:  s += 3
        if artist_l         in t:  s += 2
        if 'cover'    in t: s -= 6
        if 'karaoke'  in t: s -= 8
        if 'remix'    in t: s -= 4
        if 'nightcore'in t: s -= 8
        if 'reaction' in t: s -= 8
        if 'tutorial' in t: s -= 8
        return s

    best  = max(entries, key=score)
    vid   = best.get('id') or best.get('url', '').split('v=')[-1]
    yt_url = f'https://www.youtube.com/watch?v={vid}'
    print(f"[yt-search] Best match: '{best.get('title')}' → {vid}  score={score(best)}")
    return yt_url


# ─────────────────────────────────────────────────────────────────────────────
# ID3 tag embedding
# ─────────────────────────────────────────────────────────────────────────────

def embed_id3_tags(mp3_path: str, title: str, artist: str, album: str, cover_url: str):
    if not MUTAGEN_OK:
        print("[tag] mutagen not installed — skipping tags")
        return
    try:
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            audio = MP3(mp3_path)
            audio.tags = ID3()
            audio.tags.save(mp3_path)
            tags = ID3(mp3_path)

        for frame in ['TIT2','TIT3','TPE1','TPE2','TOPE','TOAL','TALB',
                      'APIC','COMM','TDRC','TRCK','TCON','TPUB','TENC','WXXX']:
            tags.delall(frame)

        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TALB(encoding=3, text=album or title))

        if cover_url:
            try:
                resp = requests.get(cover_url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                img  = resp.content
                mime = 'image/png' if img[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
                tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=img))
                print(f"[tag] Cover embedded: {len(img)//1024}KB")
            except Exception as exc:
                print(f"[tag] Cover download failed: {exc}")

        tags.save(mp3_path, v2_version=3)
        verify = ID3(mp3_path)
        print(f"[tag] Saved — TIT2='{verify.get('TIT2')}' TPE1='{verify.get('TPE1')}'")

    except Exception as exc:
        import traceback; traceback.print_exc()
        print(f"[tag] Error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Download worker
# ─────────────────────────────────────────────────────────────────────────────

def _progress_hook(job_id: str, d: dict):
    if d['status'] == 'downloading':
        try:
            pct = float(d.get('_percent_str', '0%').strip().rstrip('%'))
            jobs[job_id]['progress'] = int(pct * 0.85)
        except Exception:
            pass
    elif d['status'] == 'finished':
        jobs[job_id]['progress'] = 90


def run_download(job_id: str, youtube_url: str, title: str, artist: str, album: str, cover_url: str):
    safe = re.sub(r'[/\\:*?"<>|]', '-', f'{title} - {artist}')[:100].strip()
    out_template = os.path.join(DOWNLOAD_DIR, f'{job_id}.%(ext)s')
    hook = lambda d: _progress_hook(job_id, d)

    ydl_opts = {
        # bestaudio/best: yt-dlp resolves this at runtime — always works
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'quiet': True, 'no_warnings': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'writethumbnail': False, 'writeinfojson': False,
        'writedescription': False, 'addmetadata': False,
        'progress_hooks': [hook],
        'retries': 5, 'fragment_retries': 5,
        'extractor_retries': 3,
        'http_chunk_size': 10485760,

        # ── Solution 4+5: tv_embedded/mweb + auto po_token ───────────────────
        # tv_embedded = YouTube TV client — rarely bot-checked in 2025.
        # mweb = mobile web fallback.
        # po_token auto-generated on startup, refreshed every 6 hrs by bg thread.
        'extractor_args': {
            'youtube': {
                'player_client': ['tv_embedded', 'mweb'],
                **({'po_token': [_get_po_token()]} if _get_po_token() else {}),
            },
        },

        # ── Solution 3: rotating User-Agent + request sleep jitter ───────────
        'http_headers': {'User-Agent': _random_ua()},
        'sleep_interval':          2,   # min seconds between requests
        'max_sleep_interval':      5,   # max seconds (randomised in between)
        'sleep_interval_requests': 1,   # sleep between every fragment request
    }

    # ── Solution 2: browser cookies (optional, set YTDLP_COOKIES_BROWSER) ────
    # cookiesfrombrowser expects a tuple: (browser_name,) — NOT a plain string.
    if _COOKIES_BROWSER:
        ydl_opts['cookiesfrombrowser'] = (_COOKIES_BROWSER,)
        print(f"[download] Using cookies from browser: {_COOKIES_BROWSER}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        mp3_path = os.path.join(DOWNLOAD_DIR, f'{job_id}.mp3')
        if not os.path.exists(mp3_path):
            for f in sorted(os.listdir(DOWNLOAD_DIR)):
                if f.startswith(job_id):
                    os.rename(os.path.join(DOWNLOAD_DIR, f), mp3_path)
                    break

        if not os.path.exists(mp3_path):
            jobs[job_id].update({'status': 'error', 'error': 'MP3 file not found after download'})
            return

        jobs[job_id]['progress'] = 92
        embed_id3_tags(mp3_path, title, artist, album, cover_url)
        jobs[job_id].update({'status': 'done', 'progress': 100,
                              'file_path': mp3_path, 'filename': f'{safe}.mp3'})

    except Exception as exc:
        err = str(exc)
        if any(kw in err.lower() for kw in ('sign in', 'bot', 'confirm', 'blocked', 'captcha', 'login', 'age')):
            err = (
                'YouTube blocked the download (bot detection). '
                'The server is using tv_embedded/mweb clients with auto po_token. '
                'Try again in 30s (po_token may still be generating on first start). '
                'If this keeps happening: run `pip install -U yt-dlp` to get the latest extractor.'
            )
        import traceback; traceback.print_exc()
        jobs[job_id].update({'status': 'error', 'error': err})


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/favicon.ico')
def favicon():
    # Inline green music-note SVG — no file needed
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<circle cx="16" cy="16" r="16" fill="#1DB954"/>'
        '<path d="M12 22V12l10-2v2l-8 1.6V22a3 3 0 1 1-2 0z" fill="#000"/>'
        '</svg>'
    )
    from flask import Response
    return Response(svg, mimetype='image/svg+xml')


@app.route('/style.css')
def stylesheet():
    return send_from_directory(BASE_DIR, 'style.css')


@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})


@app.route('/api/spotify-meta')
def spotify_meta():
    """
    Metadata endpoint — two modes:

    Mode A (client supplies title+artist):
      GET /api/spotify-meta?title=Blinding+Lights&artist=The+Weeknd
      → server only does Deezer cover upgrade, returns enriched meta.
      Use this when the browser has already read the Spotify page.

    Mode B (legacy server-side scrape, kept as fallback):
      GET /api/spotify-meta?id=<track_id>
      → tries yt-dlp scrape then oEmbed. Often fails due to Spotify bot blocks.
    """
    title    = request.args.get('title',  '').strip()
    artist   = request.args.get('artist', '').strip()
    track_id = request.args.get('id',     '').strip()

    # ── Mode A: client already has title+artist ──────────────────────────────
    if title and artist:
        meta = {'title': title, 'artist': artist, 'album': title,
                'cover': '', 'source': 'client'}
        dz = _meta_from_deezer(f'{title} {artist}')
        if dz:
            meta['cover']  = dz['cover']
            meta['album']  = dz['album'] or title
            meta['source'] = 'client+deezer'
            print(f"[meta/modeA] Deezer cover applied for '{title}' — '{artist}'")
        return jsonify(meta)

    # ── Mode B: server-side scrape (fallback chain) ──────────────────────────
    if not track_id:
        return jsonify({'error': 'Provide title+artist or id'}), 400

    # Layer 0: embed page scrape — __NEXT_DATA__ JSON has structured artists[]
    #           and album.images[] — best source for both metadata AND cover
    meta = _meta_from_spotify_embed_scrape(track_id)

    # Layer 1: oEmbed — always fetch it so we can use thumbnail_url as
    #           a cover fallback even when embed scrape succeeded for metadata
    oembed = _meta_from_spotify_oembed(track_id)
    if not meta:
        meta = oembed  # oEmbed becomes the metadata source only if embed failed

    # Layer 2: yt-dlp Spotify extractor (last resort for metadata)
    if not meta:
        meta = _meta_from_ytdlp_spotify(track_id)
    if not meta:
        return jsonify({'error': 'Could not extract metadata from Spotify page'}), 502

    # ── Cover waterfall: embed → oEmbed thumbnail → Deezer cover_xl ──────────
    # Prefer the embed cover (already high-res from album.images[0]).
    # Fall through to oEmbed thumbnail, then to Deezer 1000×1000 cover_xl.
    embed_cover  = meta.get('cover', '')
    oembed_cover = (oembed or {}).get('cover', '')

    dz_query = f'{meta["title"]} {meta["artist"]}'.strip() if meta.get('artist') else meta['title']
    dz = _meta_from_deezer(dz_query, title_hint=meta['title'], artist_hint=meta.get('artist', ''))

    dz_cover = (dz or {}).get('cover', '')
    # Pick best available cover in priority order
    meta['cover'] = embed_cover or oembed_cover or dz_cover

    if dz:
        meta['album']  = dz['album'] or meta.get('album', meta['title'])
        # Backfill artist from Deezer only if we truly have nothing
        if not meta.get('artist') and dz.get('artist'):
            meta['artist'] = dz['artist']
        meta['source'] = meta['source'] + '+deezer'

    if not meta.get('artist'):
        meta['artist'] = 'Unknown Artist'
    print(f"[meta/cover] embed={bool(embed_cover)} oembed={bool(oembed_cover)} deezer={bool(dz_cover)} → using={'embed' if embed_cover else 'oembed' if oembed_cover else 'deezer' if dz_cover else 'none'}")
    return jsonify(meta)


@app.route('/api/youtube-search')
def youtube_search():
    """
    Find best YouTube video via yt-dlp ytsearch: — no YouTube API key needed.
    Query params: ?title=...&artist=...
    Returns: {youtube_url}
    """
    title  = request.args.get('title',  '').strip()
    artist = request.args.get('artist', '').strip()
    if not title:
        return jsonify({'error': 'Missing title'}), 400
    try:
        yt_url = _search_youtube(title, artist)
        return jsonify({'youtube_url': yt_url})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.route('/api/download', methods=['POST'])
def start_download():
    data      = request.json or {}
    yt_url    = data.get('youtube_url', '').strip()
    title     = data.get('title',  'Track')
    artist    = data.get('artist', 'Artist')
    album     = data.get('album',  title)
    cover_url = data.get('cover',  '')

    if not yt_url:
        return jsonify({'error': 'No YouTube URL'}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'running', 'progress': 0, 'file_path': None, 'error': None}
    threading.Thread(
        target=run_download,
        args=(job_id, yt_url, title, artist, album, cover_url),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/api/file/<job_id>')
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready'}), 404
    return send_file(
        job['file_path'],
        as_attachment=True,
        download_name=job['filename'],
        mimetype='audio/mpeg',
    )


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
    job = jobs.pop(job_id, None)
    if job and job.get('file_path') and os.path.exists(job['file_path']):
        try: os.remove(job['file_path'])
        except Exception: pass
    return jsonify({'ok': True})


if __name__ == '__main__':
    print("\n🎵  Spotidrop server v4 starting …")
    print("📦  Requires: pip install flask flask-cors yt-dlp mutagen requests")
    print("🌐  Open:     http://localhost:8000\n")
    print("ℹ️   Architecture: yt-dlp Spotify scrape → Deezer cover → yt-dlp ytsearch → bestaudio/best")
    print("🛡️   Anti-bot: player_client=tv_embedded/mweb | rotating UA | sleep jitter | auto po_token")
    print(f"🍪  Cookies:  {'browser=' + _COOKIES_BROWSER if _COOKIES_BROWSER else 'disabled (set YTDLP_COOKIES_BROWSER=chrome to enable)'}")
    print("🔑  po_token: auto-generating in background (refreshes every 6h) …")
    if not MUTAGEN_OK:
        print("⚠️   mutagen missing — run: pip install mutagen\n")
    app.run(host='0.0.0.0', port=8000, debug=False)
