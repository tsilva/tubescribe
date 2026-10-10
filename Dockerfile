# Base indexes and uv binary are pinned; Python packages use the committed lock.
FROM ghcr.io/astral-sh/uv:0.11.13@sha256:841c8e6fe30a8b07b4478d12d0c608cba6de66102d29d65d1cc423af86051563 AS uv
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS base
COPY --from=uv /uv /bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock uv.toml README.md ./
COPY src ./src
ENV UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 TUBESCRIBE_DATA=/scratch
RUN useradd --uid 10001 --create-home tubescribe && mkdir -p /scratch /state /objects /home/tubescribe/.cache/tubescribe/models && chown -R tubescribe:tubescribe /scratch /state /objects /home/tubescribe/.cache

FROM base AS runtime
RUN uv --config-file uv.toml sync --frozen --no-dev --no-editable --extra cloud
USER tubescribe
ENTRYPOINT ["tubescribe"]
CMD ["dashboard", "serve", "--port", "8080"]

FROM base AS gpu
# CUDA libraries arrive through locked inference wheels; supply the NVIDIA container runtime.
RUN uv --config-file uv.toml sync --frozen --no-dev --no-editable --extra asr --extra diarization
USER tubescribe
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["tubescribe.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "18765"]

FROM runtime AS rehearsal
USER root
RUN uv --config-file uv.toml sync --frozen --no-dev --no-editable --extra cloud --group rehearsal
COPY tests /app/tests
USER tubescribe
ENV TUBESCRIBE_NETWORK_TESTS=1
ENTRYPOINT ["python"]
CMD ["-m", "unittest", "discover", "-s", "tests", "-p", "test_network_runtime.py", "-v"]
