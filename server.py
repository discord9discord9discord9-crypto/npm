import os
import time
import subprocess
import threading
import requests
import signal
from flask import Flask, Response, request, render_template_string, redirect, url_for, send_file, make_response
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
# Target frames per second for the generated JPEGs. Lower default for e-ink comfort/CPU.
FRAME_FPS = float(os.environ.get("FRAME_FPS", "1.0"))
# Client refresh interval in ms (Kobo lacks native video; we "page-flip" JPEGs). Slower by default for e-ink.
FRAME_REFRESH_MS = int(os.environ.get("FRAME_REFRESH_MS", "1500"))
# JPEG quality for ffmpeg's mjpeg encoder: lower is better (4 is lightweight default)
FRAME_JPEG_QSCALE = int(os.environ.get("FRAME_JPEG_QSCALE", "4"))
# Scaling filter; fast_bilinear is lighter than the previous lanczos default.
FRAME_SCALE_FLAGS = os.environ.get("FRAME_SCALE_FLAGS", "fast_bilinear")
# Constrain ffmpeg threads so Render's 0.1 CPU quota does not get throttled.
FFMPEG_THREADS = int(os.environ.get("FFMPEG_THREADS", "1"))
# Restart ffmpeg if frames stop updating for this many seconds.
FRAME_STALE_SEC = float(os.environ.get("FRAME_STALE_SEC", "20"))
PORT = int(os.environ.get("PORT", 5000))

# Global State
current_process = None
current_streamer = None
current_quality = None
current_qscale = None
current_fps = None
last_restart_time = 0
stream_lock = threading.Lock()
restart_lock = threading.Lock()
restart_inflight = False
FRAME_PATH = "current.jpg"
FRAME_TMP_PATH = "current_tmp.jpg"
PLACEHOLDER_PATH = "placeholder.jpg"

# In-memory cache for last good frame to avoid flicker during writes
last_good_frame = None
last_good_frame_time = 0
last_good_frame_lock = threading.Lock()

# Pre-computed 1x1 white JPEG placeholder (generated once at startup)
PLACEHOLDER_JPEG = bytes([
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
    0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
    0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
    0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
    0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
    0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
    0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
    0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
    0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
    0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
    0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
    0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD3, 0x28, 0xA2, 0x80, 0x0F, 0xFF, 0xD9
])

# Twitch API Helper
class TwitchAPI:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = 0

    def get_token(self):
        if self.token and time.time() < self.token_expiry:
            return self.token
        
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        try:
            resp = requests.post(url, params=params).json()
            self.token = resp["access_token"]
            self.token_expiry = time.time() + resp["expires_in"] - 60
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
            resp = requests.get(url, headers=headers, params=params).json()
            if resp.get("data"):
                return resp["data"][0]["id"]
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
            resp = requests.get(url, headers=headers, params=params).json()
            return resp.get("data", [])
        except Exception as e:
            print(f"Error getting streams: {e}")
            return []

twitch_api = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_SECRET)

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

def process_alive():
    return current_process is not None and current_process.poll() is None

def frame_is_stale(max_age=FRAME_STALE_SEC):
    if not os.path.exists(FRAME_PATH):
        return True
    try:
        return (time.time() - os.path.getmtime(FRAME_PATH)) > max_age
    except OSError:
        return True

def read_frame_safe():
    """
    Read the current frame file safely, returning cached data if read fails.
    This prevents serving partial/corrupt JPEGs during ffmpeg writes.
    Returns (data, version) where version is the mtime for change detection.
    """
    global last_good_frame, last_good_frame_time
    try:
        mtime = os.path.getmtime(FRAME_PATH)
        with open(FRAME_PATH, "rb") as f:
            data = f.read()
        # Basic JPEG validation: must start with FFD8 and end with FFD9
        if data and len(data) > 4 and data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9':
            with last_good_frame_lock:
                last_good_frame = data
                last_good_frame_time = mtime
            return data, mtime
    except (IOError, OSError):
        pass
    # Return cached frame if current read failed
    with last_good_frame_lock:
        return last_good_frame, last_good_frame_time

def get_frame_version():
    """Return the current frame version (mtime) for polling."""
    try:
        return os.path.getmtime(FRAME_PATH)
    except OSError:
        with last_good_frame_lock:
            return last_good_frame_time

def trigger_background_restart(streamer=None, preferred_quality=None, image_qscale=None, frame_fps=None):
    """
    Prevent restart thrash by allowing only one restart thread at a time.
    """
    global restart_inflight
    if not streamer:
        return
    with restart_lock:
        if restart_inflight:
            return
        restart_inflight = True

    def _worker():
        try:
            start_stream_processing(streamer, preferred_quality, image_qscale, frame_fps)
        finally:
            with restart_lock:
                restart_inflight = False

    threading.Thread(target=_worker, daemon=True).start()

def start_stream_processing(streamer_name, preferred_quality=None, image_qscale=None, frame_fps=None):
    global current_process, current_streamer, current_quality, current_qscale, current_fps, last_restart_time
    
    with stream_lock:
        desired_quality = preferred_quality or TWITCH_STREAM_QUALITY
        desired_qscale = image_qscale or FRAME_JPEG_QSCALE
        desired_fps = frame_fps or FRAME_FPS

        if (
            current_streamer == streamer_name
            and current_quality == desired_quality
            and current_qscale == desired_qscale
            and current_fps == desired_fps
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
        ):
            return

        # Stop existing process
        if current_process:
            stop_stream_processing()
            
        current_streamer = streamer_name
        current_quality = desired_quality
        current_qscale = desired_qscale
        current_fps = desired_fps
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
                vf_parts.append(
                    f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:force_original_aspect_ratio=decrease:flags={FRAME_SCALE_FLAGS}"
                )
                vf_parts.append(
                    f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=white"
                )
            elif FRAME_WIDTH > 0:
                # Fallback: scale to width, preserve aspect.
                vf_parts.append(f"scale={FRAME_WIDTH}:-2:flags={FRAME_SCALE_FLAGS}")

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
                "-threads", str(FFMPEG_THREADS),
                "-q:v", str(desired_qscale),
                "-map_metadata", "-1",
                "-vsync", "0",
                "-flush_packets", "1",
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
    global current_process, current_streamer, current_quality, current_qscale, current_fps
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

def mjpeg_generator():
    """
    Stream the latest JPEG as a multipart/x-mixed-replace stream.
    This makes the Kobo browser render near-realtime frames without 1s polling.
    """
    boundary = b"--frame"
    min_interval = max(0.05, 1.0 / max(1.0, FRAME_FPS * 1.5))  # slightly faster than ffmpeg fps
    while True:
        try:
            data, _ = read_frame_safe()
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
            <a href="/view/{{ stream.user_name }}">
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
        #stream-container { flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; position: relative; }
        #stream-container img { position: absolute; max-width: 100%; max-height: 100%; object-fit: contain; filter: grayscale(100%); }
        .controls { padding: 10px; border-top: 1px solid #000; }
        a { text-decoration: none; color: #000; border: 1px solid #000; padding: 5px 15px; }
        select { padding: 5px; margin-right: 10px; }
        .hint { font-size: 0.9em; color: #444; padding: 8px; }
    </style>
    <script>
        // Double-buffered image loading for smooth transitions on e-ink.
        // Uses two img elements: one visible, one loading in background.
        // Swaps instantly when new frame is ready - no flicker.
        {% if autoplay %}
        (function() {
            var container = document.getElementById('stream-container');
            var imgA = document.getElementById('stream-frame');
            var imgB = document.createElement('img');
            imgB.style.visibility = 'hidden';
            imgB.alt = 'Buffer';
            container.appendChild(imgB);
            
            var active = imgA;
            var buffer = imgB;
            var loading = false;
            var seq = 0;
            
            function swap() {
                // Instant swap: hide old, show new
                active.style.visibility = 'hidden';
                buffer.style.visibility = 'visible';
                var tmp = active;
                active = buffer;
                buffer = tmp;
            }
            
            function load() {
                if (loading) return;
                loading = true;
                seq++;
                var mySeq = seq;
                
                buffer.onload = function() {
                    if (mySeq !== seq) return; // stale
                    swap();
                    loading = false;
                    setTimeout(load, {{ refresh_ms }});
                };
                buffer.onerror = function() {
                    if (mySeq !== seq) return;
                    loading = false;
                    setTimeout(load, {{ refresh_ms }});
                };
                buffer.src = '/frame.jpg?t=' + Date.now();
            }
            
            // Start loading
            load();
        })();
        {% endif %}
    </script>
</head>
<body>
    <div id="stream-container">
        {% if autoplay %}
        <img id="stream-frame" src="/frame.jpg" alt="Stream Loading...">
        {% else %}
        <div class="hint">Select a quality below to start streaming.</div>
        {% endif %}
    </div>
    <div class="controls">
        <form id="quality-form" method="get" style="display:flex; flex-direction:column; gap:6px; align-items:flex-start;">
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
    streams = twitch_api.get_streams(category)
    return render_template_string(INDEX_HTML, streams=streams, category=category)

@app.route('/view/<streamer>')
def view(streamer):
    requested_quality = request.args.get("quality")
    requested_imgq = request.args.get("imgq", type=int)
    requested_fps = request.args.get("fps", type=float)
    qualities = get_stream_qualities(streamer)

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

    selected_quality = requested_quality or (qualities[0] if qualities else TWITCH_STREAM_QUALITY)
    selected_imgq = requested_imgq or FRAME_JPEG_QSCALE
    selected_fps = requested_fps or FRAME_FPS

    image_quality_options = [
        (1, "HQ (q=1)"),
        (2, "High (q=2)"),
        (4, "Medium (q=4)"),
        (8, "Light (q=8)"),
    ]
    fps_options = [
        (0.5, "0.5 fps (very slow)"),
        (1.0, "1 fps (slow)"),
        (1.5, "1.5 fps (default)"),
        (2.0, "2 fps"),
        (3.0, "3 fps"),
        (4.0, "4 fps (faster)"),
    ]

    # Clamp refresh interval for Kobo e-ink; tie it loosely to selected fps.
    refresh_ms = request.args.get("refresh_ms", type=int)
    if refresh_ms is None:
        refresh_ms = int(max(300, min(4000, 1000.0 / max(0.1, selected_fps))))

    # Start processing only after the user has picked a quality
    autoplay = requested_quality is not None
    if autoplay:
        start_stream_processing(streamer, selected_quality, selected_imgq, selected_fps)

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
            refresh_ms=refresh_ms,
            autoplay=autoplay,
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route('/frame.jpg')
def frame():
    # Check if stream process is alive; if not and we have a target, try to restart
    global current_process, current_streamer
    if current_streamer and (not process_alive() or frame_is_stale()):
        trigger_background_restart(current_streamer, current_quality, current_qscale, current_fps)

    # Read frame safely (validates JPEG, uses cache on failure)
    data, version = read_frame_safe()
    frame_data = data if data else PLACEHOLDER_JPEG
    
    resp = make_response(frame_data)
    resp.mimetype = 'image/jpeg'
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # ETag for version tracking (client can use for smart polling)
    resp.headers["ETag"] = f'"{version:.6f}"' if version else '"0"'
    return resp

@app.route('/frame_version')
def frame_version():
    """Lightweight endpoint to check if new frame is available."""
    version = get_frame_version()
    resp = make_response(str(version))
    resp.mimetype = 'text/plain'
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route('/health')
def health():
    return "OK", 200

@app.route('/status')
def status():
    """Return current stream status for debugging."""
    return {
        "streamer": current_streamer,
        "quality": current_quality,
        "qscale": current_qscale,
        "fps": current_fps,
        "process_alive": process_alive(),
        "frame_stale": frame_is_stale(),
        "frame_version": get_frame_version(),
    }

@app.route('/stream.mjpg')
def stream_mjpg():
    # Ensure a stream is running; if not, trigger restart in background
    global current_process, current_streamer
    if current_streamer and (not process_alive() or frame_is_stale()):
        trigger_background_restart(current_streamer, current_quality, current_qscale, current_fps)

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
