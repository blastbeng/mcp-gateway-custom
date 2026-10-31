# Social Post — thin publishing MCP for YouTube Shorts, Instagram Reels,
# Facebook Reels and TikTok.
#
# No database, no queue, no scheduler: four HTTP adapters over the official
# APIs, one stdio MCP server. No system ffmpeg either: PyAV's wheel bundles
# the FFmpeg libraries we need to read duration/resolution (Debian's ffmpeg
# package alone would add ~600 MB). Alpine rather than slim: every dependency
# ships a musllinux aarch64 wheel, and the image is ~290 MB instead of ~420.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOCIALPOST_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY socialpost ./socialpost

# Tokens, credentials and history live in the /data bind mount (chmod 600).
# The gateway owns the process; this entrypoint only speaks MCP on stdio.
ENTRYPOINT ["python", "-m", "socialpost.server"]
