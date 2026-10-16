# Zookimethod server: the site, uploads and 4K 120fps processing.
# Ubuntu 24.04 ships ffmpeg 6.1 with libx265 and SVT-AV1, the versions the
# enhancement settings were benchmarked with.
FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY index.html style.css app.js upload.js serve.py ./
COPY tools/ ./tools/

# videos/ holds the posted videos, .uploads/ the upload key and uploads in progress
VOLUME ["/app/videos", "/app/.uploads"]
EXPOSE 8000
ENV PYTHONUNBUFFERED=1
STOPSIGNAL SIGTERM
CMD ["python3", "serve.py", "--port", "8000"]
