ARG NAS_BASE_IMAGE=python:3.11-slim
FROM ${NAS_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg poppler-utils libgl1 libglib2.0-0 pciutils intel-media-va-driver libmfx-gen1.2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/nas-ai-space
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.lock.txt

COPY app ./app
COPY models ./models

ENTRYPOINT []
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-server-header"]
