# Social Post — thin publishing MCP for YouTube Shorts, Instagram Reels,
# Facebook Reels and TikTok.
#
# No database, no queue, no scheduler: four HTTP adapters over the official
# APIs, one stdio MCP server. ffmpeg is only used for ffprobe (duration,
# resolution, size) so the image stays small.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOCIALPOST_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY socialpost ./socialpost

# Tokens, credentials and history live in the /data bind mount (chmod 600).
# The gateway owns the process; this entrypoint only speaks MCP on stdio.
ENTRYPOINT ["python", "-m", "socialpost.server"]
