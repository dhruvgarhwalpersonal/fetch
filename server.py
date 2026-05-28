#!/usr/bin/env python3
"""
Fetch server — v3
Audio source chain: Invidious → SoundCloud → Jiosaavn → Internet Archive
No cookies. No Piped. No YouTube direct download.
"""

import os, re, threading, uuid, time, subprocess, tempfile, shutil
from difflib import SequenceMatcher
import requests
from flask import Flask, request, jsonify, send_file, send_from_directory, Response, stream_with_context
from flask_cors import CORS

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt-dlp not installed.")
    exit(1)

try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
    from mutagen.mp3 import MP3
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

app = Flask(__name__, static_folder='.')
CORS(app)

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
MAGENTA= "\033[95m"
WHITE  = "\033[97m"

def _log(symbol, color, tag, msg):
    print(f"  {color}{BOLD}{symbol}{RESET}  {DIM}{tag:<18}{RESET} {WHITE}{msg}{RESET}")

def log_ok(tag, msg):    _log("✓", GREEN,   tag, msg)
def log_skip(tag, msg):  _log("↷", YELLOW,  tag, msg)
def log_fail(tag, msg):  _log("✗", RED,     tag, msg)
def log_info(tag, msg):  _log("·", CYAN,    tag, msg)
def log_start(tag, msg): _log("▶", BLUE,    tag, msg)
def log_done(tag, msg):  _log("★", MAGENTA, tag, msg)

def log_divider(label=""):
    if label:
        pad = (56 - len(label) - 2) // 2
        print(f"\n  {DIM}{'─'*pad} {CYAN}{BOLD}{label}{RESET}{DIM} {'─'*(56-pad-len(label)-2)}{RESET}\n")
    else:
        print(f"\n  {DIM}{'─'*56}{RESET}\n")

def _autoupdate_ytdlp():
    try:
        result = subprocess.run(
            ['pip', 'install', '--upgrade', '--quiet', 'yt-dlp'],
            capture_output=True, text=True, timeout=60
        )
        msg = result.stdout.strip() or 'already up to date'
        log_ok("yt-dlp/update", msg)
    except Exception as exc:
        log_fail("yt-dlp/update", str(exc))

threading.Thread(target=_autoupdate_ytdlp, daemon=True).start()

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs: dict = {}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
}

INVIDIOUS_INSTANCES = [
    'https://invidious.snopyta.org',
    'https://inv.riverside.rocks',
    'https://invidious.nerdvpn.de',
    'https://invidious.privacydev.net',
    'https://vid.puffyan.us',
    'https://invidious.flokinet.to',
]

_YT_ID_RE = re.compile(r'(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})')

def _extract_video_id(youtube_url: str) -> str:
    m = _YT_ID_RE.search(youtube_url)
    if not m:
        raise ValueError(f'Cannot extract video ID from: {youtube_url}')
    return m.group(1)

def _get_audio_via_invidious(video_id: str) -> str:
    errors = []
    for instance in INVIDIOUS_INSTANCES:
        try:
            url = f'{instance}/api/v1/videos/{video_id}'
            log_info("invidious", f"Trying {instance} …")
            r = requests.get(url, headers={
                **HEADERS,
                'Referer': instance + '/',
                'Origin':  instance,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()

            streams = [
                f for f in data.get('adaptiveFormats', [])
                if f.get('type', '').startswith('audio')
            ]
            if not streams:
                errors.append(f'{instance}: no audio formats')
                log_skip("invidious", f"{instance} — no audio formats")
                continue

            def _score(s):
                t = s.get('type', '').lower()
                bps = s.get('bitrate', 0) or 0
                codec = 2 if 'm4a' in t or 'mp4a' in t else (1 if 'opus' in t or 'webm' in t else 0)
                return (codec, bps)

            best = max(streams, key=_score)
            stream_url = best.get('url', '')
            if not stream_url:
                errors.append(f'{instance}: no URL on best stream')
                continue

            log_ok("invidious", f"{instance} → {best.get('type','?')} @ {best.get('bitrate','?')}bps")
            return stream_url

        except Exception as exc:
            errors.append(f'{instance}: {exc}')
            log_fail("invidious", f"{instance} — {exc}")
            continue

    raise RuntimeError(f'All Invidious instances failed for {video_id}. Errors: {"; ".join(errors)}')


def _download_audio_stream(stream_url: str, out_path: str) -> None:
    from urllib.parse import urlparse
    parsed = urlparse(stream_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    r = requests.get(stream_url, headers={
        **HEADERS,
        'Referer':         origin + '/',
        'Origin':          origin,
        'Accept':          '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }, stream=True, timeout=300, allow_redirects=True)
    r.raise_for_status()

    ct = r.headers.get('Content-Type', '')
    if 'text/html' in ct or 'text/plain' in ct:
        preview = r.content[:200].decode('utf-8', errors='replace')
        raise RuntimeError(f'Stream returned non-audio ({ct}): {preview}')

    written = 0
    with open(out_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                written += len(chunk)

    if written < 65536:
        preview = open(out_path, 'rb').read(200).decode('utf-8', errors='replace')
        raise RuntimeError(f'Stream too small ({written}B) — likely error page: {preview}')

    log_ok("stream/dl", f"{written // 1024} KB downloaded → {os.path.basename(out_path)}")


def _ffmpeg_to_mp3(raw_path: str, mp3_path: str, bitrate: str = '320k') -> None:
    result = subprocess.run([
        'ffmpeg', '-y', '-i', raw_path,
        '-vn', '-acodec', 'libmp3lame',
        '-ab', bitrate, '-ar', '44100',
        mp3_path,
    ], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f'FFmpeg failed: {result.stderr[-500:]}')
    log_ok("ffmpeg", f"Converted → {os.path.basename(mp3_path)} @ {bitrate}")


def _download_via_soundcloud(title: str, artist: str, mp3_path: str) -> bool:
    query = f'scsearch3:{title} {artist}'
    log_start("soundcloud", f"Searching: {title} — {artist}")
    tmp_dir = tempfile.mkdtemp(prefix='fetch_sc_')
    try:
        out_tpl = os.path.join(tmp_dir, 'audio.%(ext)s')
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_tpl,
            'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(query, download=True)

        for f in os.listdir(tmp_dir):
            if f.endswith('.mp3'):
                shutil.move(os.path.join(tmp_dir, f), mp3_path)
                log_ok("soundcloud", f"Downloaded → {os.path.basename(mp3_path)}")
                return True

        log_skip("soundcloud", "No MP3 found after download")
        return False

    except Exception as exc:
        log_fail("soundcloud", str(exc))
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _download_via_jiosaavn(title: str, artist: str, mp3_path: str) -> bool:
    log_start("jiosaavn", f"Searching: {title} — {artist}")
    try:
        r = requests.get(
            'https://saavn.dev/api/search/songs',
            params={'query': f'{title} {artist}', 'page': 1, 'limit': 5},
            headers=HEADERS, timeout=15,
        )
        r.raise_for_status()
        results = r.json().get('data', {}).get('results', [])
        if not results:
            log_skip("jiosaavn", "No results")
            return False

        def _score(s):
            st = (s.get('name') or '').lower()
            sa = ' '.join(a.get('name','') for a in (s.get('artists',{}).get('primary') or [])).lower()
            return SequenceMatcher(None, title.lower(), st).ratio() + SequenceMatcher(None, artist.lower(), sa).ratio() * 0.5

        best = max(results, key=_score)
        dl_urls = best.get('downloadUrl') or []
        if not dl_urls:
            log_skip("jiosaavn", "No download URLs on best result")
            return False

        quality_order = ['320kbps', '160kbps', '96kbps', '48kbps', '12kbps']
        stream_url = None
        for q in quality_order:
            for item in dl_urls:
                if item.get('quality') == q:
                    stream_url = item.get('url')
                    break
            if stream_url:
                break

        if not stream_url:
            stream_url = dl_urls[-1].get('url', '')
        if not stream_url:
            log_skip("jiosaavn", "Empty URL")
            return False

        log_info("jiosaavn", f"Found: {best.get('name')} — downloading …")
        tmp_dir = tempfile.mkdtemp(prefix='fetch_jio_')
        try:
            raw_path = os.path.join(tmp_dir, 'audio.raw')
            resp = requests.get(stream_url, headers=HEADERS, stream=True, timeout=120)
            resp.raise_for_status()
            written = 0
            with open(raw_path, 'wb') as f:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            if written < 65536:
                raise RuntimeError(f'File too small: {written}B')
            _ffmpeg_to_mp3(raw_path, mp3_path)
            log_ok("jiosaavn", f"Downloaded {written // 1024}KB → {os.path.basename(mp3_path)}")
            return True
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as exc:
        log_fail("jiosaavn", str(exc))
        return False


def _download_via_archive(title: str, artist: str, mp3_path: str) -> bool:
    log_start("archive.org", f"Searching: {title} — {artist}")
    try:
        def _search(q):
            r = requests.get(
                'https://archive.org/advancedsearch.php',
                params={'q': q, 'fl[]': ['identifier','title','creator'],
                        'rows': 5, 'page': 1, 'output': 'json'},
                headers=HEADERS, timeout=15,
            )
            r.raise_for_status()
            return r.json().get('response', {}).get('docs', [])

        docs = _search(f'title:({title}) AND creator:({artist}) AND mediatype:audio') \
            or _search(f'{title} {artist} mediatype:audio')

        if not docs:
            log_skip("archive.org", "No results found")
            return False

        identifier = docs[0].get('identifier', '')
        if not identifier:
            log_skip("archive.org", "No identifier in result")
            return False

        meta_r = requests.get(f'https://archive.org/metadata/{identifier}', headers=HEADERS, timeout=15)
        meta_r.raise_for_status()
        files = meta_r.json().get('files', [])

        audio_files = [f for f in files if f.get('name','').lower().endswith(('.mp3','.ogg','.flac','.m4a'))]
        if not audio_files:
            log_skip("archive.org", f"No audio files in item {identifier}")
            return False

        mp3s = [f for f in audio_files if f['name'].lower().endswith('.mp3')]
        chosen = mp3s[0] if mp3s else audio_files[0]
        file_url = f'https://archive.org/download/{identifier}/{chosen["name"]}'

        log_info("archive.org", f"Downloading: {chosen['name']} …")
        tmp_dir = tempfile.mkdtemp(prefix='fetch_arch_')
        try:
            raw_path = os.path.join(tmp_dir, 'audio.raw')
            resp = requests.get(file_url, headers=HEADERS, stream=True, timeout=300)
            resp.raise_for_status()
            written = 0
            with open(raw_path, 'wb') as f:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            if written < 65536:
                raise RuntimeError(f'File too small: {written}B')
            if chosen['name'].lower().endswith('.mp3'):
                shutil.move(raw_path, mp3_path)
            else:
                _ffmpeg_to_mp3(raw_path, mp3_path)
            log_ok("archive.org", f"Downloaded {written // 1024}KB → {os.path.basename(mp3_path)}")
            return True
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except Exception as exc:
        log_fail("archive.org", str(exc))
        return False


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _best_thumbnail(thumbnails: list) -> str:
    valid = [t for t in thumbnails if t.get('url')]
    if not valid:
        return ''
    return sorted(valid, key=lambda t: t.get('width', 0) * t.get('height', 0), reverse=True)[0]['url']


def _meta_from_spotify_oembed(track_id: str) -> dict | None:
    try:
        r = requests.get('https://open.spotify.com/oembed',
                         params={'url': f'https://open.spotify.com/track/{track_id}'},
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        title_raw = data.get('title', '')
        cover     = data.get('thumbnail_url', '')
        title, artist = title_raw, ''
        if ' \u00b7 ' in title_raw:
            parts = title_raw.split(' \u00b7 ', 1)
            title = parts[0].strip(); artist = parts[1].strip()
        elif ' by ' in title_raw:
            parts = title_raw.rsplit(' by ', 1)
            title = parts[0].strip(); artist = parts[1].strip()
        if not title:
            return None
        log_ok("meta/oembed", f"title='{title}' artist='{artist}'")
        return {'title': title, 'artist': artist, 'album': title, 'cover': cover, 'source': 'oembed'}
    except Exception as exc:
        log_fail("meta/oembed", str(exc))
        return None


def _meta_from_spotify_embed_scrape(track_id: str) -> dict | None:
    import json as _json
    url = f'https://open.spotify.com/embed/track/{track_id}'
    try:
        r = requests.get(url, headers={**HEADERS, 'Accept': 'text/html', 'Referer': 'https://open.spotify.com/'}, timeout=15)
        r.raise_for_status()
        html = r.text

        nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd_match:
            try:
                nd = _json.loads(nd_match.group(1))
                entity = (
                    nd.get('props',{}).get('pageProps',{}).get('state',{}).get('data',{}).get('entity') or
                    nd.get('props',{}).get('pageProps',{}).get('track') or
                    nd.get('props',{}).get('initialState',{}).get('data',{}).get('entity')
                )
                if entity:
                    name = entity.get('name','') or entity.get('title','')
                    raw_artists = entity.get('artists',[]) or entity.get('artist',[])
                    artist = ', '.join(a['name'] for a in raw_artists if isinstance(a, dict) and a.get('name')) if isinstance(raw_artists, list) else str(raw_artists)
                    album_obj = entity.get('album',{}) or {}
                    album = album_obj.get('name','') or name
                    images = album_obj.get('images',[]) or entity.get('images',[])
                    cover = images[0].get('url','') if images else ''
                    if name and artist:
                        log_ok("meta/embed", f"title='{name}' artist='{artist}'")
                        return {'title': name, 'artist': artist, 'album': album, 'cover': cover, 'source': 'embed-nextdata'}
            except Exception as exc:
                log_fail("meta/embed", f"__NEXT_DATA__ parse: {exc}")

        def _og(prop):
            m = (re.search(rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"', html) or
                 re.search(rf'<meta[^>]+content="([^"]+)"[^>]+property="{prop}"', html))
            return m.group(1) if m else ''

        og_t = _og('og:title'); og_d = _og('og:description'); og_i = _og('og:image')
        if og_t:
            title  = og_t.split('·')[0].split('•')[0].strip()
            artist = ''
            parts  = [p.strip() for p in re.split(r'[·•]', og_d) if p.strip()]
            if len(parts) >= 2:
                artist = parts[1]
            elif ' by ' in og_d.lower():
                artist = og_d.lower().split(' by ')[1].split(' on ')[0].strip().title()
            if title:
                log_ok("meta/embed-og", f"title='{title}' artist='{artist}'")
                return {'title': title, 'artist': artist, 'album': title, 'cover': og_i, 'source': 'embed-og'}

        log_fail("meta/embed", "Could not parse embed HTML")
        return None
    except Exception as exc:
        log_fail("meta/embed", str(exc))
        return None


def _meta_from_deezer(query: str, title_hint: str = '', artist_hint: str = '') -> dict | None:
    try:
        r = requests.get('https://api.deezer.com/search',
                         params={'q': query, 'limit': '8'}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        tracks = r.json().get('data', [])
        if not tracks:
            return None
        th = (title_hint or query.split()[0]).lower()
        ah = artist_hint.lower()

        def _dz_score(c):
            ct = (c.get('title') or c.get('title_short','')).lower()
            ca = (c.get('artist',{}).get('name','')).lower()
            return _similarity(ct, th)*10 + (_similarity(ca, ah)*10 if ah else 0)

        best = max(tracks, key=_dz_score)
        title  = best.get('title') or best.get('title_short','')
        artist = best.get('artist',{}).get('name','')
        album  = best.get('album',{}).get('title', title)
        alb    = best.get('album',{})
        cover  = alb.get('cover_xl') or alb.get('cover_big') or alb.get('cover_medium') or alb.get('cover') or ''
        if not title or not artist:
            return None
        log_ok("meta/deezer", f"title='{title}' artist='{artist}' score={_dz_score(best):.1f}")
        return {'title': title, 'artist': artist, 'album': album, 'cover': cover, 'source': 'deezer'}
    except Exception as exc:
        log_fail("meta/deezer", str(exc))
        return None


def _meta_from_ytdlp_spotify(track_id: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True,
                                'skip_download': True, 'extract_flat': False, 'noplaylist': True}) as ydl:
            info = ydl.extract_info(f'https://open.spotify.com/track/{track_id}', download=False)
        if not info:
            return None
        title  = info.get('track')  or info.get('title')   or ''
        artist = info.get('artist') or info.get('creator') or info.get('uploader') or ''
        album  = info.get('album')  or title
        cover  = _best_thumbnail(info.get('thumbnails') or []) or info.get('thumbnail','')
        if not title:
            return None
        log_ok("meta/yt-dlp", f"title='{title}' artist='{artist}'")
        return {'title': title, 'artist': artist, 'album': album, 'cover': cover, 'source': 'ytdlp-spotify'}
    except Exception as exc:
        log_fail("meta/yt-dlp", str(exc))
        return None


def _search_youtube(title: str, artist: str) -> str:
    query  = f'{title} {artist} official audio'
    log_start("yt-search", f"{title} — {artist}")
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True,
            'skip_download': True, 'extract_flat': True, 'noplaylist': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }) as ydl:
            results = ydl.extract_info(f'ytsearch5:{query}', download=False)
    except Exception as exc:
        raise RuntimeError(f'yt-dlp search failed: {exc}')

    entries = (results or {}).get('entries') or []
    if not entries:
        raise RuntimeError('No YouTube results found.')

    title_l = title.lower(); artist_l = artist.lower()

    def score(e):
        t  = (e.get('title') or '').lower()
        ch = (e.get('uploader') or e.get('channel') or '').lower()
        s  = 0
        if ch.replace(' - topic','').strip() == artist_l: s += 12
        if artist_l in ch or ch.replace(' vevo','') in artist_l: s += 8
        if 'official audio' in t: s += 8
        if 'official video' in t: s += 6
        if 'official'       in t: s += 3
        if 'audio'          in t: s += 2
        if title_l          in t: s += 3
        if artist_l         in t: s += 2
        if 'cover'     in t: s -= 6
        if 'karaoke'   in t: s -= 8
        if 'remix'     in t: s -= 4
        if 'nightcore' in t: s -= 8
        if 'reaction'  in t: s -= 8
        return s

    best   = max(entries, key=score)
    vid    = best.get('id') or best.get('url','').split('v=')[-1]
    yt_url = f'https://www.youtube.com/watch?v={vid}'
    log_ok("yt-search", f"'{best.get('title')}' → {vid}  score={score(best)}")
    return yt_url


def embed_id3_tags(mp3_path: str, title: str, artist: str, album: str, cover_url: str):
    if not MUTAGEN_OK:
        log_skip("id3", "mutagen not installed — skipping tags")
        return
    try:
        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
            audio = MP3(mp3_path)
            audio.tags = ID3()
            audio.tags.save(mp3_path)
            tags = ID3(mp3_path)

        for frame in ['TIT2','TIT3','TPE1','TPE2','TOPE','TOAL','TALB','APIC','COMM','TDRC','TRCK','TCON','TPUB','TENC','WXXX']:
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
                log_ok("id3/cover", f"{len(img)//1024}KB embedded")
            except Exception as exc:
                log_fail("id3/cover", str(exc))

        tags.save(mp3_path, v2_version=3)
        log_ok("id3", f"Tags saved — '{title}' by '{artist}'")
    except Exception as exc:
        log_fail("id3", str(exc))


def _try_invidious_download(youtube_url: str, mp3_path: str) -> bool:
    try:
        video_id   = _extract_video_id(youtube_url)
        stream_url = _get_audio_via_invidious(video_id)
        tmp_dir    = tempfile.mkdtemp(prefix='fetch_inv_')
        try:
            raw_path = os.path.join(tmp_dir, 'audio.raw')
            _download_audio_stream(stream_url, raw_path)
            _ffmpeg_to_mp3(raw_path, mp3_path)
            return True
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        log_fail("invidious", str(exc))
        return False


def run_download(job_id: str, youtube_url: str, title: str, artist: str, album: str, cover_url: str):
    safe     = re.sub(r'[/\\:*?"<>|]', '-', f'{title} - {artist}')[:100].strip()
    mp3_path = os.path.join(DOWNLOAD_DIR, f'{job_id}.mp3')

    log_divider(f"JOB {job_id}")
    log_info("job", f"{title} — {artist}")

    log_start("source/1", "Invidious")
    jobs[job_id]['progress'] = 20
    if _try_invidious_download(youtube_url, mp3_path):
        log_ok("source/1", "Invidious succeeded ✓")
    else:
        log_start("source/2", "SoundCloud")
        jobs[job_id]['progress'] = 35
        if _download_via_soundcloud(title, artist, mp3_path):
            log_ok("source/2", "SoundCloud succeeded ✓")
        else:
            log_start("source/3", "Jiosaavn")
            jobs[job_id]['progress'] = 55
            if _download_via_jiosaavn(title, artist, mp3_path):
                log_ok("source/3", "Jiosaavn succeeded ✓")
            else:
                log_start("source/4", "Internet Archive")
                jobs[job_id]['progress'] = 70
                if _download_via_archive(title, artist, mp3_path):
                    log_ok("source/4", "Archive.org succeeded ✓")
                else:
                    log_fail("job", "All 4 sources failed")
                    jobs[job_id].update({'status': 'error', 'error': 'All audio sources failed. This track may not be available.'})
                    return

    if not os.path.exists(mp3_path):
        jobs[job_id].update({'status': 'error', 'error': 'MP3 file missing after download'})
        return

    jobs[job_id]['progress'] = 90
    embed_id3_tags(mp3_path, title, artist, album, cover_url)
    jobs[job_id].update({'status': 'done', 'progress': 100,
                         'file_path': mp3_path, 'filename': f'{safe}.mp3'})
    log_done("job", f"Complete → {safe}.mp3")
    log_divider()


def _run_stream_download(yt_url: str, title: str, artist: str, tmp_dir: str) -> str:
    mp3_path = os.path.join(tmp_dir, 'audio.mp3')

    log_divider(f"STREAM {title[:30]}")
    log_info("stream", f"{title} — {artist}")

    log_start("source/1", "Invidious")
    if _try_invidious_download(yt_url, mp3_path):
        log_ok("source/1", "Invidious succeeded ✓")
        return mp3_path

    log_start("source/2", "SoundCloud")
    if _download_via_soundcloud(title, artist, mp3_path):
        log_ok("source/2", "SoundCloud succeeded ✓")
        return mp3_path

    log_start("source/3", "Jiosaavn")
    if _download_via_jiosaavn(title, artist, mp3_path):
        log_ok("source/3", "Jiosaavn succeeded ✓")
        return mp3_path

    log_start("source/4", "Internet Archive")
    if _download_via_archive(title, artist, mp3_path):
        log_ok("source/4", "Archive.org succeeded ✓")
        return mp3_path

    raise RuntimeError('All audio sources exhausted — track unavailable.')


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/favicon.ico')
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<circle cx="16" cy="16" r="16" fill="#1DB954"/>'
           '<path d="M12 22V12l10-2v2l-8 1.6V22a3 3 0 1 1-2 0z" fill="#000"/>'
           '</svg>')
    return Response(svg, mimetype='image/svg+xml')

@app.route('/style.css')
def stylesheet():
    return send_from_directory('.', 'style.css')

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True})

@app.route('/api/spotify-meta')
def spotify_meta():
    title    = request.args.get('title',  '').strip()
    artist   = request.args.get('artist', '').strip()
    track_id = request.args.get('id',     '').strip()

    if title and artist:
        meta = {'title': title, 'artist': artist, 'album': title, 'cover': '', 'source': 'client'}
        dz = _meta_from_deezer(f'{title} {artist}')
        if dz:
            meta['cover']  = dz['cover']
            meta['album']  = dz['album'] or title
            meta['source'] = 'client+deezer'
        return jsonify(meta)

    if not track_id:
        return jsonify({'error': 'Provide title+artist or id'}), 400

    log_divider("METADATA")
    meta   = _meta_from_spotify_embed_scrape(track_id)
    oembed = _meta_from_spotify_oembed(track_id)
    if not meta:
        meta = oembed
    if not meta:
        meta = _meta_from_ytdlp_spotify(track_id)
    if not meta:
        return jsonify({'error': 'Could not extract metadata from Spotify'}), 502

    embed_cover  = meta.get('cover','')
    oembed_cover = (oembed or {}).get('cover','')
    dz_query     = f'{meta["title"]} {meta.get("artist","")}'.strip()
    dz           = _meta_from_deezer(dz_query, title_hint=meta['title'], artist_hint=meta.get('artist',''))
    dz_cover     = (dz or {}).get('cover','')
    meta['cover'] = embed_cover or oembed_cover or dz_cover

    if dz:
        meta['album']  = dz['album'] or meta.get('album', meta['title'])
        if not meta.get('artist') and dz.get('artist'):
            meta['artist'] = dz['artist']
        meta['source'] = meta['source'] + '+deezer'

    if not meta.get('artist'):
        meta['artist'] = 'Unknown Artist'

    log_info("meta/cover", f"embed={bool(embed_cover)} oembed={bool(oembed_cover)} deezer={bool(dz_cover)}")
    return jsonify(meta)


@app.route('/api/youtube-search')
def youtube_search():
    title  = request.args.get('title',  '').strip()
    artist = request.args.get('artist', '').strip()
    if not title:
        return jsonify({'error': 'Missing title'}), 400
    try:
        yt_url = _search_youtube(title, artist)
        return jsonify({'youtube_url': yt_url})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.route('/api/stream-download', methods=['POST'])
def stream_download():
    data      = request.json or {}
    yt_url    = data.get('youtube_url', '').strip()
    title     = data.get('title', 'Track')
    artist    = data.get('artist', 'Artist')

    if not yt_url:
        return jsonify({'error': 'No YouTube URL'}), 400

    safe_name = re.sub(r'[/\\:*?"<>|]', '-', f'{title} - {artist}')[:100].strip()
    filename  = f'{safe_name}.mp3'
    tmp_dir   = tempfile.mkdtemp(prefix='fetch_')

    try:
        mp3_path  = _run_stream_download(yt_url, title, artist, tmp_dir)
        file_size = os.path.getsize(mp3_path)
        log_info("stream", f"{filename} — {file_size // 1024}KB — sending to client")

        def generate():
            try:
                with open(mp3_path, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return Response(
            stream_with_context(generate()),
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': str(file_size),
                'X-Accel-Buffering': 'no',
            }
        )
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log_fail("stream", str(exc))
        return jsonify({'error': str(exc)}), 500


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
    return send_file(job['file_path'], as_attachment=True,
                     download_name=job['filename'], mimetype='audio/mpeg')


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
    job = jobs.pop(job_id, None)
    if job and job.get('file_path') and os.path.exists(job['file_path']):
        try: os.remove(job['file_path'])
        except Exception: pass
    return jsonify({'ok': True})


if __name__ == '__main__':
    print()
    print(f"  {MAGENTA}{BOLD}★  Fetch server v3{RESET}")
    print(f"  {DIM}Audio chain: Invidious → SoundCloud → Jiosaavn → Archive.org{RESET}")
    print(f"  {DIM}Open: http://localhost:8000{RESET}")
    print()
    if not MUTAGEN_OK:
        log_fail("startup", "mutagen missing — run: pip install mutagen")
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
