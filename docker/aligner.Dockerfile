# Aligner image for Concordance: CPU torch + ctc-forced-aligner + ffmpeg.
# Build: docker build -f docker/aligner.Dockerfile -t concordance-aligner:dev .
# The model weights are not baked in; mount a cache at /cache and they download
# once to /cache/hf (~1.26 GB).
FROM python:3.12-slim

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg git g++ \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Pinned to the commit benchmarked on 2026-09-16. Install from git, not PyPI:
# the PyPI package "ctc-forced-aligner" is a different fork (deskpai).
ARG CFA_COMMIT=64293cc6d711e57666c4a8b098e9fd93b381fd88
RUN pip install --no-cache-dir "git+https://github.com/MahmoudAshraf97/ctc-forced-aligner.git@${CFA_COMMIT}"

ENV HF_HOME=/cache/hf \
    CONCORDANCE_CACHE_DIR=/cache/alignments \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY concordance /app/concordance
ENTRYPOINT ["python", "-m", "concordance.aligner"]
