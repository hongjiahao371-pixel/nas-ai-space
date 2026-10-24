FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates libgomp1 libcurl4t64 \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/embeddinggemma2-manifest.json /opt/manifest.json
COPY docker/prepare-multimodal.py /opt/prepare.py
ENV PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/models/runtime
ENTRYPOINT ["python3", "/opt/prepare.py"]
