# Klaket worker — ffmpeg + faster-whisper + sherpa-onnx media pipeline
# (upstream apps/worker).
#
# No torch on purpose: transcription is CTranslate2 (faster-whisper), speaker
# diarization is sherpa-onnx (pyannote segmentation + 3dspeaker ERes2Net
# embeddings), OCR is rapidocr/onnxruntime, scene detection is OpenCV. All of
# them ship aarch64 wheels, so the build is a pure download on this Pi.
#
# `argostranslate` is removed from upstream requirements: it drags in
# torch + spacy + stanza (1.5-2 GB) for a transcript-translation feature that
# the MCP server does not even expose (klaket_ingest only takes `url`).
# pipeline.py imports the translate module lazily and treats a failure as
# fail-soft, so dropping the dependency only disables translate_to.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/models \
    XDG_DATA_HOME=/models \
    KLAKET_MODEL_DIR=/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/klaket

RUN git clone --depth 1 --branch v0.7.1 \
        https://github.com/huseyinstif/klaket.git .

WORKDIR /opt/klaket/apps/worker

# REDIS_DB: upstream jobstore.connect() only reads REDIS_ADDR and always uses
# database 0. Klaket shares the host-wide Redis instance, whose db0/db1/db2 are
# taken, so make the index selectable. Same patch/guard combination as the API.
RUN sed -i 's/port=int(port or 6379),/port=int(port or 6379),\n        db=int(os.environ.get("REDIS_DB", "0")),/' \
        klaket_worker/jobstore.py \
    && grep -q 'REDIS_DB' klaket_worker/jobstore.py

RUN sed -i '/^argostranslate/d' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

# faster-whisper 1.2.x still calls av.open(..., metadata_errors="ignore"),
# a kwarg PyAV 19 removed: without this pin every transcription dies with
# "open() got an unexpected keyword argument 'metadata_errors'". 16.x is the
# last major that accepts it and ships a cp312 aarch64 wheel.
RUN pip install --no-cache-dir "av==16.0.1"

# Models (whisper, sherpa-onnx, OCR) and job artifacts live on bind mounts
# under /opt: the container's writable layer stays small and nothing lands in
# /var/lib/docker on the root filesystem.
ENTRYPOINT ["python", "-m", "klaket_worker"]
