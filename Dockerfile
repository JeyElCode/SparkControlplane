# syntax=docker/dockerfile:1

# --- Stage 1: build the React SPA ---------------------------------------
# Pinned to the BUILD platform, never the target — and it must stay that way.
#
# THE RULE: only architecture-neutral files may leave this stage. Its single
# consumer is the `COPY --from=frontend .../dist` below, and `dist/` is the JS,
# CSS and HTML vite emits, which is byte-identical whatever CPU produced it.
# Adding a native frontend dependency (a .node addon, sharp, lightningcss) or a
# second COPY --from that lifts node_modules or any binary would silently place
# build-platform code inside the published arm64 image. Nothing enforces this
# rule automatically except the arm64 smoke test in release.yml — keep it.
#
# WHY, because the evidence is counter-intuitive and this looks revertible:
# building the SPA for arm64 means running npm under QEMU user-mode emulation,
# and QEMU *intermittently* raises SIGILL inside npm's child processes. When it
# does, a surviving emulated child keeps the step's process group alive and
# BuildKit waits on a container that will never exit — no error, no output, until
# GitHub kills the job at its six-hour maximum. It is intermittent, not
# deterministic: the same layer, under the same emulator, completed in ~41s on
# two runs and took SIGILL on two others. Do not conclude from one green
# emulated build that this is fixed upstream.
#
# It cost three six-hour jobs before anyone saw it, and two of them were
# releases that consequently never published an image at all — v1.14.0 and
# v1.25.0 are the only gaps in the published tag sequence. `|| npm install` was
# half the mechanism: npm ci took SIGILL and exited non-zero, the `||` swallowed
# that as if it were a lockfile problem, and npm install restarted and took its
# own SIGILL. A lockfile is committed, so `npm ci` is now mandatory — the
# fallback could only ever mask a real mismatch by installing different
# dependency versions into a release image.
#
# The runtime stage below is deliberately NOT pinned, so it still builds per
# target and the published manifest stays genuinely multi-arch.
#
# Note: `FROM --platform=` requires BuildKit. `DOCKER_BUILDKIT=0 docker build .`
# fails at parse time; use `docker buildx build` or Compose v2.
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: backend runtime -------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SPARK_DATA_DIR=/data \
    SPARK_FRONTEND_DIR=/app/frontend/dist

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -m -d /home/spark spark

WORKDIR /app/backend
COPY backend/ ./
RUN pip install --no-cache-dir .

# Built SPA served by the API
COPY --from=frontend /app/frontend/dist /app/frontend/dist

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data && chown -R spark:spark /data

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/api/health').status==200 else 1)"

# Entrypoint chowns /data then drops to the unprivileged 'spark' user via gosu.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
