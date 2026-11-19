# Usher: standard library Python, plus cryptography for Web Push.
# Base image: Python 3.15. A comment can't follow FROM on the same line: Docker reads it as arguments.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

# The base image can lag Debian's security fixes for zlib; take the fixed package at build time. A build that reuses
# this cached layer keeps the earlier zlib until the layer is rebuilt. Remove the step once a base image digest carries the fix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    USHER_WEB=/app/web

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY CHANGELOG.md .
COPY usher/ usher/
COPY web/ web/

# The data folder belongs to the user below, so a new named volume mounted there (compose.yaml) starts out writable by it;
# a folder mounted from the host (deploy/compose.yaml) is not affected.
RUN mkdir /data && chown 1000:1001 /data

# Runs as the media user and group on the host (1000:1001 by default); the compose file also makes the filesystem read-only.
USER 1000:1001

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "usher.server"]
