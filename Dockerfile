# syntax=docker/dockerfile:1

# ---- Build stage: has git + the GitHub token, installs deps into a venv ----
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is needed to install the private `events` package from GitHub
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies first so this layer is cached between code changes.
# The GitHub token is mounted as a BuildKit secret only for this step and is
# never written to an image layer (see `secrets` in docker-compose.yml).
COPY requirements.txt .
RUN --mount=type=secret,id=git_credentials,target=/root/.git-credentials \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=credential.helper \
    GIT_CONFIG_VALUE_0=store \
    pip install -r requirements.txt

# ---- Runtime stage: no git, no token, just the installed venv ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Application code (.env is excluded via .dockerignore; secrets come from env_file at runtime)
COPY config.py .
COPY app ./app

# Run as an unprivileged user
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
