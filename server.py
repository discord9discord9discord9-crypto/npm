import os
import time
import subprocess
import threading
import requests
import signal
import math
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, Response, request, render_template_string, send_file, make_response
from streamlink import Streamlink
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Environment Variables
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_SECRET = os.environ.get("TWITCH_SECRET")
TWITCH_CATEGORY = os.environ.get("TWITCH_CATEGORY", "Just Chatting")
TWITCH_STREAM_QUALITY = os.environ.get("TWITCH_STREAM_QUALITY", "best")
# Kobo Elipsa panel is 1404x1872 (portrait). Kobo browser may not rotate to landscape,
# so we optionally rotate frames server-side to make "device-rotated" viewing work.
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "1404"))
FRAME_HEIGHT = int(os.environ.get("FRAME_HEIGHT", "1872"))
# "cw" (clockwise), "ccw" (counter-clockwise), or "none"
FRAME_ROTATE = os.environ.get("FRAME_ROTATE", "cw").strip().lower()
# Target frames per second for the generated JPEGs.
# Keep this reasonably low for bandwidth/CPU, but not so low that motion is unusable.
FRAME_FPS = float(os.environ.get("FRAME_FPS", "1.0"))
# Client refresh interval in ms (Kobo lacks native video; we "page-flip" JPEGs). Slower by default for e-ink.
FRAME_REFRESH_MS = int(os.environ.get("FRAME_REFRESH_MS", "1500"))
# JPEG quality for ffmpeg's mjpeg encoder: lower is better (2 ~= very high quality).
# Kobo readability benefits from keeping this relatively high quality.
FRAME_JPEG_QSCALE = int(os.environ.get("FRAME_JPEG_QSCALE", "2"))
# Default viewer preset: "low", "balanced", or "hq".
# Default to balanced so Kobo readability stays high by default.
BANDWIDTH_PRESET = os.environ.get("BANDWIDTH_PRESET", "balanced").strip().lower()
# If ffmpeg stalls but doesn't exit, restart when the frame file stops updating.
FRAME_STALE_SECONDS = float(os.environ.get("FRAME_STALE_SECONDS", "12"))
PORT = int(os.environ.get("PORT", 5000))

# Network tuning (important for slow/unreliable internet)
HTTP_CONNECT_TIMEOUT = float(os.environ.get("HTTP_CONNECT_TIMEOUT", "3.0"))
HTTP_READ_TIMEOUT = float(os.environ.get("HTTP_READ_TIMEOUT", "8.0"))
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", "2"))

# Global State
current_process = None
current_streamer = None
current_quality = None
current_qscale = None
current_fps = None
current_scale = None
last_restart_time = 0
stream_lock = threading.Lock()
FRAME_PATH = "current.jpg"
PLACEHOLDER_PATH = "placeholder.jpg"

# Small in-memory caches to reduce network work on slow links
_STREAMS_CACHE = {"ts": 0.0, "category": None, "data": []}
_QUALITIES_CACHE = {}  # streamer -> {"ts": float, "data": [qualities]}

def _build_requests_session() -> requests.Session:
    """
    Requests session with conservative retries/backoff for flaky links.
    Keeps connections alive (faster) and avoids hanging forever (timeouts).
    """
    s = requests.Session()
    retry = Retry(
        total=max(0, HTTP_RETRIES),
        connect=max(0, HTTP_RETRIES),
        read=max(0, HTTP_RETRIES),
        status=max(0, HTTP_RETRIES),
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "kobo-twitch/1.0"})
    return s

# Twitch API Helper
class TwitchAPI:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = 0
        self.session = _build_requests_session()

    def get_token(self):
        if self.token and time.time() < self.token_expiry:
            return self.token
        
        url = "https://id.twitch.tv/oauth2/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        try:
            r = self.session.post(
                url,
                data=data,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            )
            payload = r.json() if r is not None else {}
            if r is None or r.status_code >= 400:
                raise RuntimeError(f"token request failed ({getattr(r, 'status_code', 'n/a')}): {payload}")
            self.token = payload["access_token"]
            self.token_expiry = time.time() + int(payload.get("expires_in", 0)) - 60
            return self.token
        except Exception as e:
            print(f"Error getting token: {e}")
            return None

    def get_game_id(self, game_name):
        token = self.get_token()
        if not token: return None
        
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}"
        }
        url = "https://api.twitch.tv/helix/games"
        params = {"name": game_name}
        
        try:
            r = self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            )
            payload = r.json() if r is not None else {}
            if r is None or r.status_code >= 400:
                raise RuntimeError(f"games lookup failed ({getattr(r, 'status_code', 'n/a')}): {payload}")
            if payload.get("data"):
                return payload["data"][0]["id"]
        except Exception as e:
            print(f"Error getting game ID: {e}")
        return None

    def get_streams(self, game_name):
        game_id = self.get_game_id(game_name)
        if not game_id:
            return []
            
        token = self.get_token()
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}"
        }
        url = "https://api.twitch.tv/helix/streams"
        params = {"game_id": game_id, "first": 20}
        
        try:
            r = self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            )
            payload = r.json() if r is not None else {}
            if r is None or r.status_code >= 400:
                raise RuntimeError(f"streams lookup failed ({getattr(r, 'status_code', 'n/a')}): {payload}")
            return payload.get("data", [])
        except Exception as e:
            print(f"Error getting streams: {e}")
            return []

twitch_api = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_SECRET)

def _clamp(n, lo, hi):
    return max(lo, min(hi, n))

def _even_int(n, minimum=2):
    n = int(n)
    n = max(minimum, n)
    return n if (n % 2 == 0) else (n - 1)

def _preset_defaults(preset: str):
    """
    Defaults tuned for "really bad internet".
    Note: ffmpeg mjpeg qscale: lower is higher quality (and bigger).
    """
    preset = (preset or "").strip().lower()
    if preset == "hq":
        return {"quality": "best", "imgq": 2, "fps": 2.0, "scale": 1.0, "mode": "poll"}
    if preset == "balanced":
        return {"quality": TWITCH_STREAM_QUALITY, "imgq": FRAME_JPEG_QSCALE, "fps": FRAME_FPS, "scale": 1.0, "mode": "poll"}
    # Default: low
    # Important for Kobo: avoid downscaling by default (it makes text/UI blurry).
    # Save bandwidth primarily via fewer frames + lower stream rendition.
    return {"quality": "worst", "imgq": 4, "fps": 0.5, "scale": 1.0, "mode": "poll"}


def _ensure_placeholder():
    """
    Create a simple placeholder JPEG so the Kobo always receives a valid image.
    Using ffmpeg keeps deps minimal (no Pillow).
    """
    if os.path.exists(PLACEHOLDER_PATH):
        return
    try:
        # Plain white placeholder, sized to the configured frame canvas.
        w = max(64, int(FRAME_WIDTH) if FRAME_WIDTH > 0 else 1404)
        h = max(64, int(FRAME_HEIGHT) if FRAME_HEIGHT > 0 else 1872)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=white:s={w}x{h}:r=1",
                "-frames:v",
                "1",
                "-vf",
                "format=gray",
                "-q:v",
                "6",
                PLACEHOLDER_PATH,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        # If placeholder creation fails, we'll fall back to 404.
        pass


def _frame_is_stale(path: str, stale_seconds: float) -> bool:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True
    except Exception:
        return False
    age = time.time() - st.st_mtime
    return age > stale_seconds

def _frame_etag_and_mtime(path: str):
    """
    ETag based on file mtime+size (fast) for conditional requests.
    Returns (etag, mtime_float) or (None, None).
    """
    try:
        st = os.stat(path)
    except Exception:
        return None, None
    # Return the *unquoted* tag; Werkzeug will quote and apply weakness.
    etag = f"{int(st.st_mtime_ns):x}-{int(st.st_size):x}"
    return etag, st.st_mtime

def cached_get_streams(category: str, ttl_seconds: float = 30.0):
    now = time.time()
    if _STREAMS_CACHE["category"] == category and (now - _STREAMS_CACHE["ts"]) < ttl_seconds:
        return _STREAMS_CACHE["data"]
    data = twitch_api.get_streams(category)
    _STREAMS_CACHE.update({"ts": now, "category": category, "data": data})
    return data

def cached_get_stream_qualities(streamer_name: str, ttl_seconds: float = 300.0):
    now = time.time()
    entry = _QUALITIES_CACHE.get(streamer_name)
    if entry and (now - entry["ts"]) < ttl_seconds:
        return entry["data"]
    data = get_stream_qualities(streamer_name)
    _QUALITIES_CACHE[streamer_name] = {"ts": now, "data": data}
    return data

def create_streamlink_session():
    """
    Configure Streamlink to minimize buffering/memory use inside the 512MB container.
    """
    session = Streamlink()
    session.set_option("hls-live-edge", 1)              # keep only the newest segments
    session.set_option("hls-segment-threads", 1)        # avoid parallel segment downloads
    session.set_option("stream-segment-threads", 1)
    session.set_option("hls-segment-queue-size", 2)     # keep a tiny in-memory queue
    session.set_option("hls-playlist-reload-attempts", 2)
    session.set_option("hls-segment-attempts", 2)
    return session

def get_stream_qualities(streamer_name):
    """
    Return available qualities for the streamer with minimal buffering.
    """
    session = create_streamlink_session()
    try:
        streams = session.streams(f"twitch.tv/{streamer_name}")
        return list(streams.keys()) if streams else []
    except Exception as e:
        print(f"Error listing qualities for {streamer_name}: {e}")
        return []
    finally:
        # Close HTTP session to release sockets/memory promptly
        try:
            session.http.close()
        except Exception:
            pass

def pick_stream(streams, desired_quality):
    """
    Pick the best available stream object preferring the user choice, then sane fallbacks.
    """
    order = [
        desired_quality,
        "source",
        "1080p60",
        "1080p",
        "best",
        "720p60",
        "720p",
        "480p",
        "360p",
        "worst",
    ]
    for q in order:
        if q and q in streams:
            return q, streams[q]
    # Final fallback: first available
    if streams:
        q = next(iter(streams.keys()))
        return q, streams[q]
    return None, None

def start_stream_processing(streamer_name, preferred_quality=None, image_qscale=None, frame_fps=None, frame_scale=None):
    global current_process, current_streamer, current_quality, current_qscale, current_fps, current_scale, last_restart_time
    
    with stream_lock:
        desired_quality = preferred_quality or TWITCH_STREAM_QUALITY
        desired_qscale = image_qscale or FRAME_JPEG_QSCALE
        desired_fps = frame_fps or FRAME_FPS
        desired_scale = frame_scale if frame_scale is not None else 1.0
        desired_scale = float(_clamp(desired_scale, 0.25, 1.0))

        if (
            current_streamer == streamer_name
            and current_quality == desired_quality
            and current_qscale == desired_qscale
            and current_fps == desired_fps
            and current_scale == desired_scale
            and current_process
            and current_process.poll() is None
        ):
            return # Already watching this streamer at requested quality
        
        # Rate limit restarts for the same streamer/quality (e.g., 10 seconds)
        if (
            time.time() - last_restart_time < 10
            and current_streamer == streamer_name
            and current_quality == desired_quality
            and current_qscale == desired_qscale
            and current_fps == desired_fps
            and current_scale == desired_scale
        ):
            return

        # Stop existing process
        if current_process:
            stop_stream_processing()
            
        current_streamer = streamer_name
        current_quality = desired_quality
        current_qscale = desired_qscale
        current_fps = desired_fps
        current_scale = desired_scale
        last_restart_time = time.time()
        
        # Get Stream URL using Streamlink
        session = create_streamlink_session()
        try:
            streams = session.streams(f"twitch.tv/{streamer_name}")
            if not streams:
                print(f"No streams found for {streamer_name}")
                # Don't unset current_streamer, so we can retry later via frame()
                return
            
            quality_used, stream_obj = pick_stream(streams, desired_quality)
            if not stream_obj:
                print(f"No usable stream qualities found for {streamer_name}: {list(streams.keys())}")
                return
            stream_url = stream_obj.url
            current_quality = quality_used
            current_qscale = desired_qscale
            current_fps = desired_fps
            
            # Start ffmpeg
            # -i <url>: Input
            # -vf "fps=1,format=gray": 1 frame per second, grayscale
            # -y: Overwrite output
            # -update 1: Continously update the image file
            #
            # IMPORTANT: avoid aggressive downscaling; it makes on-screen text blurry.
            vf_parts = [f"fps={desired_fps}"]
            if FRAME_ROTATE in ("cw", "clockwise", "90"):
                vf_parts.append("transpose=1")
            elif FRAME_ROTATE in ("ccw", "counterclockwise", "counter-clockwise", "-90", "270"):
                vf_parts.append("transpose=2")

            if FRAME_WIDTH > 0 and FRAME_HEIGHT > 0:
                # Fit within the Kobo's portrait canvas (or your configured canvas).
                # If rotated, this makes sideways-holding the device effectively "landscape".
                target_w = FRAME_WIDTH
                target_h = FRAME_HEIGHT
                if desired_scale != 1.0:
                    target_w = _even_int(math.floor(FRAME_WIDTH * desired_scale))
                    target_h = _even_int(math.floor(FRAME_HEIGHT * desired_scale))
                vf_parts.append(
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=lanczos"
                )
                vf_parts.append(
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=white"
                )
            elif FRAME_WIDTH > 0:
                # Fallback: scale to width, preserve aspect.
                target_w = FRAME_WIDTH
                if desired_scale != 1.0:
                    target_w = _even_int(math.floor(FRAME_WIDTH * desired_scale))
                vf_parts.append(f"scale={target_w}:-2:flags=lanczos")

            # Kobo is grayscale; force grayscale output.
            vf_parts.append("format=gray")
            vf_parts.append("setsar=1")
            vf = ",".join(vf_parts)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-analyzeduration", "0",
                "-probesize", "32k",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-rtbufsize", "16M",
                "-re", # Read input at native frame rate (important for live streams)
                "-i", stream_url,
                "-vf", vf,
                "-q:v", str(desired_qscale),
                "-map_metadata", "-1",
                "-vsync", "0",
                "-flush_packets", "1",
                # Prevent partially-written JPEG reads (atomic temp + rename)
                "-f", "image2",
                "-atomic_writing", "1",
                "-update", "1",
                FRAME_PATH
            ]
            
            # Run in background
            current_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Started ffmpeg for {streamer_name}")
            
        except Exception as e:
            print(f"Error starting stream: {e}")
            # Keep current_streamer set to allow retries
        finally:
            try:
                session.http.close()
            except Exception:
                pass

def stop_stream_processing():
    global current_process, current_streamer, current_quality, current_qscale, current_fps, current_scale
    if current_process:
        current_process.terminate()
        try:
            current_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current_process.kill()
        current_process = None
    current_streamer = None
    current_quality = None
    current_qscale = None
    current_fps = None
    current_scale = None

def mjpeg_generator():
    """
    Stream the latest JPEG as a multipart/x-mixed-replace stream.
    This makes the Kobo browser render near-realtime frames without 1s polling.
    """
    boundary = b"--frame"
    min_interval = max(0.05, 1.0 / max(1.0, FRAME_FPS * 1.5))  # slightly faster than ffmpeg fps
    last_mtime = 0.0
    while True:
        try:
            if os.path.exists(FRAME_PATH):
                try:
                    mtime = os.path.getmtime(FRAME_PATH)
                except Exception:
                    mtime = 0.0
                if mtime > last_mtime:
                    last_mtime = mtime
                    with open(FRAME_PATH, "rb") as f:
                        data = f.read()
                    if data:
                        yield boundary + b"\r\n"
                        yield b"Content-Type: image/jpeg\r\n"
                        yield b"Cache-Control: no-store, no-cache, must-revalidate\r\n"
                        yield b"Pragma: no-cache\r\n"
                        yield f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
                        yield data + b"\r\n"
            time.sleep(min_interval)
        except GeneratorExit:
            break
        except Exception as e:
            print(f"mjpeg stream error: {e}")
            time.sleep(0.5)

# Templates
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kobo Twitch</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <style>
        body { font-family: sans-serif; background: #fff; color: #000; padding: 10px; }
        h1 { font-size: 1.5em; text-align: center; }
        .stream-list { list-style: none; padding: 0; }
        .stream-item { border-bottom: 1px solid #ccc; padding: 10px 0; }
        .stream-item a { text-decoration: none; color: #000; display: block; }
        .stream-title { font-weight: bold; font-size: 1.1em; }
        .stream-meta { font-size: 0.9em; color: #555; }
        .refresh-btn { display: block; width: 100%; padding: 10px; background: #eee; border: 1px solid #000; text-align: center; text-decoration: none; color: #000; margin-bottom: 20px; }
    </style>
</head>
<body>
    <h1>Twitch: {{ category }}</h1>
    <a href="/" class="refresh-btn">Refresh List</a>
    <ul class="stream-list">
        {% for stream in streams %}
        <li class="stream-item">
            <a href="/view/{{ stream.user_name }}?preset={{ default_preset }}&quality={{ default_quality }}&imgq={{ default_imgq }}&fps={{ default_fps }}&scale={{ default_scale }}&mode={{ default_mode }}">
                <div class="stream-title">{{ stream.user_name }}</div>
                <div class="stream-meta">{{ stream.viewer_count }} viewers - {{ stream.title }}</div>
            </a>
        </li>
        {% else %}
        <li class="stream-item">No streams found or API error.</li>
        {% endfor %}
    </ul>
</body>
</html>
"""

VIEW_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ streamer }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <style>
        body { margin: 0; padding: 0; background: #fff; text-align: center; height: 100vh; display: flex; flex-direction: column; }
        #stream-container { flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        img { max-width: 100%; max-height: 100%; object-fit: contain; filter: grayscale(100%); }
        .controls { padding: 10px; border-top: 1px solid #000; }
        a { text-decoration: none; color: #000; border: 1px solid #000; padding: 5px 15px; }
        select { padding: 5px; margin-right: 10px; }
        .hint { font-size: 0.9em; color: #444; padding: 8px; }
    </style>
    <script>
        // Slow-link safe loop:
        // Only request the next frame AFTER the current one finishes downloading.
        // This prevents canceling in-flight downloads (common cause of "stuck on one frame").
        {% if autoplay and mode == 'poll' %}
        (function () {
            var refreshMs = {{ refresh_ms }};
            var img = document.getElementById('stream-frame');
            if (!img) return;

            var loading = false;
            var watchdog = null;
            var toggle = 0;

            function scheduleNext(delay) {
                window.setTimeout(kick, delay);
            }

            function kick() {
                if (!img) return;
                if (loading) return; // wait for onload/onerror
                loading = true;
                // Keep URL mostly stable so the browser can revalidate with ETag (304).
                // Toggle between two URLs to ensure the <img> reloads even if src repeats.
                toggle = 1 - toggle;
                img.src = '/frame.jpg?v=' + toggle;

                // If the request hangs forever, force a retry.
                if (watchdog) window.clearTimeout(watchdog);
                watchdog = window.setTimeout(function () {
                    if (loading) {
                        loading = false;
                        scheduleNext(0);
                    }
                }, Math.max(20000, refreshMs * 6));
            }

            img.onload = function () {
                loading = false;
                scheduleNext(refreshMs);
            };
            img.onerror = function () {
                loading = false;
                scheduleNext(Math.min(2000, refreshMs));
            };

            scheduleNext(0);
        })();
        {% endif %}
    </script>
</head>
<body>
    <div id="stream-container">
        {% if autoplay %}
            {% if mode == 'mjpg' %}
            <img id="stream-frame" src="/stream.mjpg" alt="Stream Loading...">
            {% else %}
            <img id="stream-frame" src="/frame.jpg?v=0" alt="Stream Loading...">
            {% endif %}
        {% else %}
        <div class="hint">Select a quality below to start streaming.</div>
        {% endif %}
    </div>
    <div class="controls">
        <form id="quality-form" method="get" style="display:flex; flex-direction:column; gap:6px; align-items:flex-start;">
            <input type="hidden" name="preset" value="{{ preset }}">
            <div>
                <label for="quality">Stream:</label>
                <select name="quality" id="quality" onchange="this.form.submit()">
                    {% for q in qualities %}
                    <option value="{{ q }}" {% if q == selected_quality %}selected{% endif %}>{{ q }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label for="imgq">Image:</label>
                <select name="imgq" id="imgq" onchange="this.form.submit()">
                    {% for qv, qlabel in image_quality_options %}
                    <option value="{{ qv }}" {% if qv == selected_imgq %}selected{% endif %}>{{ qlabel }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label for="fps">FPS:</label>
                <select name="fps" id="fps" onchange="this.form.submit()">
                    {% for fv, flabel in fps_options %}
                    <option value="{{ fv }}" {% if fv == selected_fps %}selected{% endif %}>{{ flabel }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label for="scale">Size:</label>
                <select name="scale" id="scale" onchange="this.form.submit()">
                    {% for sv, slabel in scale_options %}
                    <option value="{{ sv }}" {% if sv == selected_scale %}selected{% endif %}>{{ slabel }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label for="mode">Mode:</label>
                <select name="mode" id="mode" onchange="this.form.submit()">
                    {% for mv, mlabel in mode_options %}
                    <option value="{{ mv }}" {% if mv == mode %}selected{% endif %}>{{ mlabel }}</option>
                    {% endfor %}
                </select>
            </div>
            <noscript><button type="submit">Apply</button></noscript>
        </form>
        <a href="/">Back to List</a>
        <span>{{ streamer }}</span>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    category = TWITCH_CATEGORY
    streams = cached_get_streams(category)
    defaults = _preset_defaults(BANDWIDTH_PRESET)
    return render_template_string(
        INDEX_HTML,
        streams=streams,
        category=category,
        default_preset=BANDWIDTH_PRESET,
        default_quality=defaults["quality"],
        default_imgq=defaults["imgq"],
        default_fps=defaults["fps"],
        default_scale=defaults["scale"],
        default_mode=defaults["mode"],
    )

@app.route('/view/<streamer>')
def view(streamer):
    preset = request.args.get("preset", BANDWIDTH_PRESET)
    defaults = _preset_defaults(preset)

    requested_quality = request.args.get("quality")
    requested_imgq = request.args.get("imgq", type=int)
    requested_fps = request.args.get("fps", type=float)
    requested_scale = request.args.get("scale", type=float)
    mode = (request.args.get("mode") or defaults["mode"]).strip().lower()
    mode = "mjpg" if mode == "mjpg" else "poll"

    qualities = cached_get_stream_qualities(streamer)

    # Always include the configured default and common fallbacks, and dedupe.
    baseline_qualities = [
        TWITCH_STREAM_QUALITY,
        "source",
        "1080p60",
        "1080p",
        "720p60",
        "720p",
        "480p",
        "360p",
        "worst",
    ]
    qualities = list(dict.fromkeys((qualities or []) + baseline_qualities))

    selected_quality = requested_quality or (qualities[0] if qualities else defaults["quality"])
    selected_imgq = requested_imgq or int(defaults["imgq"])
    selected_fps = requested_fps or float(defaults["fps"])
    selected_scale = requested_scale or float(defaults["scale"])
    selected_scale = float(_clamp(selected_scale, 0.25, 1.0))

    image_quality_options = [
        (1, "HQ (q=1)"),
        (2, "High (q=2)"),
        (4, "Medium (q=4)"),
        (8, "Light (q=8)"),
    ]
    fps_options = [
        (0.5, "0.5 fps (very slow)"),
        (1.0, "1 fps (slow)"),
        (1.5, "1.5 fps"),
        (2.0, "2 fps"),
        (3.0, "3 fps"),
        (4.0, "4 fps (faster)"),
    ]
    scale_options = [
        (1.0, "100% (full size)"),
        (0.85, "85% (smaller)"),
        (0.7, "70% (low bandwidth)"),
        (0.55, "55% (very low bandwidth)"),
    ]
    mode_options = [
        ("poll", "Polling (best compatibility)"),
        ("mjpg", "MJPEG (1 connection)"),
    ]

    # Clamp refresh interval for Kobo e-ink; tie it loosely to selected fps.
    refresh_ms = request.args.get("refresh_ms", type=int)
    if refresh_ms is None:
        refresh_ms = int(max(300, min(4000, 1000.0 / max(0.1, selected_fps))))

    # Start processing only after the user has picked a quality
    autoplay = requested_quality is not None
    if autoplay:
        start_stream_processing(streamer, selected_quality, selected_imgq, selected_fps, selected_scale)

    resp = make_response(
        render_template_string(
            VIEW_HTML,
            streamer=streamer,
            qualities=qualities,
            selected_quality=selected_quality,
            image_quality_options=image_quality_options,
            selected_imgq=selected_imgq,
            fps_options=fps_options,
            selected_fps=selected_fps,
            scale_options=scale_options,
            selected_scale=selected_scale,
            mode_options=mode_options,
            mode=mode,
            refresh_ms=refresh_ms,
            autoplay=autoplay,
            preset=preset,
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route('/frame.jpg')
def frame():
    # Check if stream process is alive or frames are stale; if so, restart in background.
    global current_process, current_streamer, current_quality, current_qscale, current_fps, current_scale
    if current_streamer:
        dead = (not current_process) or (current_process.poll() is not None)
        stale = _frame_is_stale(FRAME_PATH, FRAME_STALE_SECONDS)
        if dead or stale:
            threading.Thread(
                target=start_stream_processing,
                args=(current_streamer, current_quality, current_qscale, current_fps, current_scale),
                daemon=True,
            ).start()

    # If the file exists, serve it with conditional request support (ETag/304).
    if os.path.exists(FRAME_PATH):
        etag, mtime = _frame_etag_and_mtime(FRAME_PATH)
        resp = send_file(FRAME_PATH, mimetype="image/jpeg", conditional=False, etag=False, last_modified=mtime)
        if etag:
            resp.set_etag(etag, weak=True)
            # Apply conditional logic *after* setting ETag/Last-Modified.
            resp.make_conditional(request)
        # Allow caching but force revalidation. This enables 304s (huge win on slow links).
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp
    else:
        _ensure_placeholder()
        if os.path.exists(PLACEHOLDER_PATH):
            etag, mtime = _frame_etag_and_mtime(PLACEHOLDER_PATH)
            resp = send_file(PLACEHOLDER_PATH, mimetype="image/jpeg", conditional=False, etag=False, last_modified=mtime)
            if etag:
                resp.set_etag(etag, weak=True)
                resp.make_conditional(request)
            resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            resp.headers["X-Accel-Buffering"] = "no"
            return resp
        return "Loading...", 404

@app.route('/health')
def health():
    return "OK", 200

@app.route('/stream.mjpg')
def stream_mjpg():
    # Ensure a stream is running; if not, trigger restart in background
    global current_process, current_streamer
    if current_streamer and (not current_process or current_process.poll() is not None):
        threading.Thread(target=start_stream_processing, args=(current_streamer, current_quality, current_qscale, current_fps, current_scale)).start()

    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame", headers=headers)

# Cleanup on exit
def cleanup(signum, frame):
    stop_stream_processing()
    exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

if __name__ == '__main__':
    # Clean start
    if os.path.exists(FRAME_PATH):
        os.remove(FRAME_PATH)
        
    app.run(host='0.0.0.0', port=PORT)
