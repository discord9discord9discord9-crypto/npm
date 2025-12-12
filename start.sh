#!/bin/bash

if [ -z "$TWITCH_CLIENT_ID" ]; then
    echo "WARNING: TWITCH_CLIENT_ID is not set."
fi

if [ -z "$TWITCH_SECRET" ]; then
    echo "WARNING: TWITCH_SECRET is not set."
fi

# Run with Gunicorn
# 1 worker is essential because we use global state for the ffmpeg process
echo "Starting Kobo Twitch Server on port $PORT..."
exec gunicorn server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

