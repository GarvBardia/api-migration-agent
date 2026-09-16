# Backend image (Step 1 gap, closed in the containerization session,
# 2026-08-27) -- runs the FastAPI app, the Celery worker, and Celery beat
# (three services in docker-compose.yml, one shared image/build).
#
# Design fork, decided this session (documented in full in CLAUDE.md):
# install the Docker CLI binary via apt rather than refactor validation.py
# to use docker-py. The existing subprocess-based code in
# app/agents/validation.py keeps working completely unchanged -- lower risk
# than touching already-verified Step 5 code, per the kickoff's own steer.
FROM python:3.12-slim

# libpq-dev + build-essential: needed for psycopg (binary wheel usually
# covers this, but pgvector/psycopg[binary] still wants libpq at runtime on
# slim images) -- gcc kept in case any dependency needs to compile.
# docker.io + docker-cli: gives us a real `docker` CLI binary matching
# validation.py's `_docker_binary()` lookup (`shutil.which("docker")`) --
# on Linux there's no `docker` vs `docker.exe` ambiguity like on Windows,
# just the one binary.
#
# **Real bug found and fixed here (2026-09-06, live container verification
# session) -- documented in full, dated, in CLAUDE.md.** On this base
# image's Debian version (python:3.12-slim -> Debian "trixie"/13), the
# `docker.io` package's `Depends:` only pulls in the DAEMON (`dockerd`,
# `docker-proxy`, `docker-init`) -- the actual CLI client binary
# (`/usr/bin/docker`) ships in a SEPARATE `docker-cli` package, reached
# only via `docker.io`'s `Recommends:`, not `Depends:`. This Dockerfile's
# original `--no-install-recommends` (a reasonable minimal-image default)
# silently strips that -- the daemon installs fine, `dpkg -l` shows
# `docker.io` as installed, but `/usr/bin/docker` never exists and
# `shutil.which("docker")` returns None. Confirmed live: a real fixture
# migration through the fully containerized stack got past a real
# `migrate_task` (successful OpenRouter LLM call, ~33s) and then
# `validate_task` failed immediately with `SandboxSetupError: Could not
# locate the docker CLI`, verified via `docker exec ... which docker`
# (empty) and `dpkg -L docker.io` (no `bin/docker` entry) before this fix.
# Fixed by explicitly listing `docker-cli` alongside `docker.io` --
# `--no-install-recommends` stays in place for everything else (still a
# deliberate minimal-image choice), this just stops relying on a
# Recommends-only relationship for a binary the code actually needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    docker.io \
    docker-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
