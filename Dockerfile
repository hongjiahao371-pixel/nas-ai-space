FROM python:3.12-slim-bookworm AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY scripts/build-sqlite.py /tmp/build-sqlite.py
RUN python /tmp/build-sqlite.py /sqlite

FROM python:3.12-slim-bookworm
COPY --from=sqlite-build /sqlite/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 51, 3)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript ffmpeg poppler-utils libgl1 libglib2.0-0 pciutils fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY scripts/pin-search-pages.py /usr/local/bin/nas-ai-pin-search-pages.py
COPY scripts/pin-runtime-code.py /usr/local/bin/nas-ai-pin-runtime-code.py
COPY app ./app
COPY models ./models
COPY scripts/restore-recovery.py ./scripts/restore-recovery.py

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-server-header"]
