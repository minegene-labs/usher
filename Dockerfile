# Usher: standard library Python, plus cryptography for Web Push.
# Base image: Python 3.13. A comment can't follow FROM on the same line: Docker reads it as arguments.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    USHER_WEB=/app/web

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY CHANGELOG.md .
COPY usher/ usher/
COPY web/ web/

# Runs as the media user and group on the host (1000:1001 by default); the compose file also makes the filesystem read-only.
USER 1000:1001

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "usher.server"]
