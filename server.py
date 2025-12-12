import os
import time
import subprocess
import threading
import requests
import signal
from flask import Flask, Response, request, render_template_string, redirect, url_for, send_file
from streamlink import Streamlink
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Environment Variables
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_SECRET = os.environ.get("TWITCH_SECRET")
TWITCH_CATEGORY = os.environ.get("TWITCH_CATEGORY", "Just Chatting")
TWITCH_STREAM_QUALITY = os.environ.get("TWITCH_STREAM_QUALITY", "best")
# Kobo Elipsa is 1404x1872; default to native portrait width for crisp text.
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH", "1404"))
# JPEG quality for ffmpeg's mjpeg encoder: lower is better (2 ~= very high quality)
FRAME_JPEG_QSCALE = int(os.environ.get("FRAME_JPEG_QSCALE", "2"))
PORT = int(os.environ.get("PORT", 5000))

# Global State
current_process = None
current_streamer = None
last_restart_time = 0
stream_lock = threading.Lock()
FRAME_PATH = "current.jpg"
PLACEHOLDER_PATH = "placeholder.jpg"

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

def start_stream_processing(streamer_name):
    global current_process, current_streamer, last_restart_time
    
    with stream_lock:
        if current_streamer == streamer_name and current_process and current_process.poll() is None:
            return # Already watching this streamer
        
        # Rate limit restarts (e.g., 10 seconds)
        if time.time() - last_restart_time < 10:
             return

        # Stop existing process
        if current_process:
            stop_stream_processing()
            
        current_streamer = streamer_name
        last_restart_time = time.time()
        
        # Get Stream URL using Streamlink
        session = Streamlink()
        try:
            streams = session.streams(f"twitch.tv/{streamer_name}")
            if not streams:
                print(f"No streams found for {streamer_name}")
                # Don't unset current_streamer, so we can retry later via frame()
                return
            
            # Prefer higher quality for readability on e-ink (text clarity).
            # Allow overriding via TWITCH_STREAM_QUALITY (e.g., "best", "1080p", "720p", "worst").
            stream_obj = streams.get(TWITCH_STREAM_QUALITY) or streams.get("best") or streams.get("720p") or streams.get("480p") or streams.get("worst")
            if not stream_obj:
                print(f"No usable stream qualities found for {streamer_name}: {list(streams.keys())}")
                return
            stream_url = stream_obj.url
            
            # Start ffmpeg
            # -i <url>: Input
            # -vf "fps=1,format=gray": 1 frame per second, grayscale
            # -y: Overwrite output
            # -update 1: Continously update the image file
            #
            # IMPORTANT: avoid aggressive downscaling; it makes on-screen text blurry.
            vf_parts = ["fps=1", "format=gray"]
            if FRAME_WIDTH > 0:
                # High-quality downscale to Kobo-ish width; -2 preserves aspect ratio and makes even dimensions.
                vf_parts.append(f"scale={FRAME_WIDTH}:-2:flags=lanczos")
            vf = ",".join(vf_parts)
            cmd = [
                "ffmpeg",
                "-y",
                "-re", # Read input at native frame rate (important for live streams)
                "-i", stream_url,
                "-vf", vf,
                "-q:v", str(FRAME_JPEG_QSCALE),
                "-update", "1",
                FRAME_PATH
            ]
            
            # Run in background
            current_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Started ffmpeg for {streamer_name}")
            
        except Exception as e:
            print(f"Error starting stream: {e}")
            # Keep current_streamer set to allow retries

def stop_stream_processing():
    global current_process, current_streamer
    if current_process:
        current_process.terminate()
        try:
            current_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current_process.kill()
        current_process = None
    current_streamer = None

# Templates
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Kobo Twitch</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
    <style>
        body { margin: 0; padding: 0; background: #fff; text-align: center; height: 100vh; display: flex; flex-direction: column; }
        #stream-container { flex: 1; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        img { max-width: 100%; max-height: 100%; object-fit: contain; filter: grayscale(100%); }
        .controls { padding: 10px; border-top: 1px solid #000; }
        a { text-decoration: none; color: #000; border: 1px solid #000; padding: 5px 15px; }
    </style>
    <script>
        function refreshImage() {
            var img = document.getElementById('stream-frame');
            img.src = '/frame.jpg?t=' + new Date().getTime();
        }
        setInterval(refreshImage, 1000);
        
        // Auto-reload page on error (offline detection fallback)
        function handleError() {
            console.log("Image load error, retrying...");
            setTimeout(refreshImage, 2000);
        }
    </script>
</head>
<body>
    <div id="stream-container">
        <img id="stream-frame" src="/frame.jpg" onerror="handleError()" alt="Stream Loading...">
    </div>
    <div class="controls">
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
    # Start processing in background if not already
    start_stream_processing(streamer)
    return render_template_string(VIEW_HTML, streamer=streamer)

@app.route('/frame.jpg')
def frame():
    # Check if stream process is alive; if not and we have a target, try to restart
    global current_process, current_streamer
    if current_streamer and (not current_process or current_process.poll() is not None):
        # Trigger restart in background to avoid blocking request? 
        # Or just do it here (it's fast enough to spawn)
        # We need to run it in a thread to not block the request?
        # start_stream_processing handles rate limiting.
        threading.Thread(target=start_stream_processing, args=(current_streamer,)).start()

    # If the file exists, serve it.
    if os.path.exists(FRAME_PATH):
        resp = send_file(FRAME_PATH, mimetype='image/jpeg')
        # Kobo / embedded browsers can be aggressive about caching; force a fresh fetch.
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    else:
        # Return a placeholder or 404
        # Create a simple placeholder if it doesn't exist
        # For now, just return 404 or a "Loading" text image?
        # A 404 might trigger the onerror in JS
        return "Loading...", 404

@app.route('/health')
def health():
    return "OK", 200

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
