# syntax=docker/dockerfile:1
# Pin the base by digest at release time: PY_IMAGE=python:3.12-slim@sha256:...
ARG PY_IMAGE=python:3.12-slim

FROM ${PY_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.runtime.lock /tmp/requirements.lock
RUN python -m venv /venv \
 && /venv/bin/pip install --require-hashes -r /tmp/requirements.lock

FROM ${PY_IMAGE}
ARG VERSION=dev
ARG REVISION=unknown
# Apply Debian security updates newer than the base image.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
LABEL org.opencontainers.image.title="thermabot" \
      org.opencontainers.image.source="https://github.com/comment23/thermabot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PORT=8095 TZ=America/New_York
COPY --from=build /venv /venv
WORKDIR /app
COPY thermabot/ /app/thermabot/
RUN mkdir -p /data && chown 99:100 /data
USER 99:100
EXPOSE 8095
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/healthz',timeout=4).status==200 else 1)"]
CMD ["uvicorn", "thermabot.app.main:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "1", "--no-access-log"]
