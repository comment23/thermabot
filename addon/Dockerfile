# syntax=docker/dockerfile:1
# Home Assistant add-on image. Extends the hassio-addons base so Supervisor's
# ingress expectations are met, then installs the same app from the same requirements
# lock as the plain-Docker image (Dockerfile at repo root). The plain-Docker path is
# untouched by this file.
#
# Supervisor does not build this file. addon/config.yaml sets `image:`, and Supervisor
# pulls ghcr.io/comment23/thermabot-addon-{arch}:<version>. CI publishes that image with
# the repository root as the build context (`docker build --file addon/Dockerfile .`).
# The COPY paths below are root-relative on purpose. `docker build addon/` is the context
# Supervisor would use only when `image` is absent, and it is not a supported install path.
ARG BUILD_FROM=ghcr.io/hassio-addons/base:21.0.10
FROM ${BUILD_FROM}
ARG VERSION=dev
ARG REVISION=unknown

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PORT=8095

# Supervisor passes BUILD_ARCH (amd64, aarch64, ...). Default keeps a local amd64 build working.
ARG BUILD_ARCH=amd64
LABEL \
    io.hass.version="${VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="thermabot" \
    org.opencontainers.image.source="https://github.com/comment23/thermabot" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${REVISION}"

# Argon2 bindings are sdist-only on Alpine; remove build tools and unused base utilities.
COPY requirements.runtime.lock /tmp/requirements.lock
RUN apk add --no-cache python3 py3-pip \
 && apk add --no-cache --virtual .build-deps build-base \
 && /usr/bin/python3 -m venv /venv \
 && /venv/bin/pip install --require-hashes -r /tmp/requirements.lock \
 && /venv/bin/pip uninstall --yes pip setuptools \
 && apk del --no-cache .build-deps py3-pip py3-msgpack py3-setuptools py3-urllib3 \
 && rm -f /tmp/requirements.lock /usr/bin/tempio

ENV PATH=/venv/bin:$PATH
WORKDIR /app
COPY thermabot/ /app/thermabot/
COPY addon/run.sh /run.sh
COPY addon/options_to_env.py /app/options_to_env.py
RUN chmod a+x /run.sh

EXPOSE 8095
CMD ["/run.sh"]
