# syntax=docker/dockerfile:1

# Python 3.14 is used in every environment — local, CI and this image — so that
# "works on my machine" and "works in prod" are the same claim. All pinned
# dependencies resolve to native cp314 wheels, so no compiler is needed.
ARG PYTHON_VERSION=3.14

# --------------------------------------------------------------------------- #
# Stage 1: build the virtualenv.
#
# Separated so the final image carries neither pip's cache nor any build tool.
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone, before the source, so a code change does not invalidate the
# dependency layer — the slowest step in the build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------- #
# Stage 2: runtime.
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH"

# Runs as a non-root user with no login shell. If the process is compromised,
# it does not own the filesystem it is running on.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /srv

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations

USER app

# DigitalOcean App Platform injects PORT; 8080 is its default.
ENV PORT=8080
EXPOSE 8080

# Reports the container's own view of readiness. The platform health check
# hits /health over HTTP as well — this one matters for `docker run` locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4).status == 200 else 1)"

# exec form, so uvicorn is PID 1 and receives SIGTERM directly — without it,
# a shell swallows the signal and the platform waits out the kill timeout on
# every deploy. Single worker per container: concurrency scales by instance
# count on App Platform, and multiple workers would multiply DB pools.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log"]
