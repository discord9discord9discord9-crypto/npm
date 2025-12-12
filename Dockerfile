FROM python:3.11-slim

# Install system dependencies: ffmpeg is crucial
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make start script executable
RUN chmod +x start.sh

# Environment variables defaults
ENV PORT=5000
ENV TWITCH_CATEGORY="Just Chatting"

# Expose the port
EXPOSE $PORT

# Run the start script
CMD ["./start.sh"]

