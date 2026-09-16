# syntax=docker/dockerfile:1

# The frontend output is architecture-independent. Build it on the native
# Buildx platform so npm and Vite do not run under QEMU for arm64 images.
FROM --platform=$BUILDPLATFORM node:22.13.0-alpine AS frontend

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts

COPY frontend/ ./
RUN npm run build


FROM ghcr.io/linuxserver/unrar:latest AS unrar

FROM ghcr.io/linuxserver/baseimage-alpine:3.23

ARG BUILD_DATE
ARG VERSION=development
ARG BAZARR_VERSION=development
ARG VCS_REF=unknown
ARG SOURCE_REPOSITORY=unknown

LABEL org.opencontainers.image.title="Bazarr with bilingual subtitle support" \
      org.opencontainers.image.description="Bazarr with first-class Chinese-English bilingual subtitle profiles" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}" \
      build_version="Bazarr bilingual ${VERSION} Build-date:- ${BUILD_DATE}"

ENV TZ="Etc/UTC" \
    BAZARR_VERSION="${BAZARR_VERSION}"

RUN apk add --no-cache --virtual=build-dependencies \
        build-base \
        cargo \
        libffi-dev \
        libpq-dev \
        libxml2-dev \
        libxslt-dev \
        python3-dev && \
    apk add --no-cache \
        ffmpeg \
        libxml2 \
        libxslt \
        mediainfo \
        python3 && \
    mkdir -p /app/bazarr/bin

WORKDIR /app/bazarr/bin

COPY bazarr.py requirements.txt postgres-requirements.txt ./
COPY bazarr ./bazarr
COPY custom_libs ./custom_libs
COPY libs ./libs
COPY migrations ./migrations
COPY --from=frontend /build/frontend/build ./frontend/build

RUN python3 -m venv /lsiopy && \
    pip install -U --no-cache-dir pip wheel && \
    pip install -U --no-cache-dir \
        --find-links https://wheel-index.linuxserver.io/alpine-3.23/ \
        -r requirements.txt \
        -r postgres-requirements.txt && \
    mkdir -p /app/bazarr && \
    printf 'UpdateMethod=docker\nBranch=master\nPackageVersion=%s\nPackageAuthor=bazarr-bilingual-fork\n' \
        "${VERSION}" > /app/bazarr/package_info && \
    printf 'Bazarr bilingual image: %s\nBuild-date: %s\nRevision: %s\n' \
        "${VERSION}" "${BUILD_DATE}" "${VCS_REF}" > /build_version && \
    apk del --purge build-dependencies && \
    rm -rf /root/.cache /root/.cargo /tmp/*

COPY docker/root/ /
COPY --from=unrar /usr/bin/unrar-alpine /usr/bin/unrar

EXPOSE 6767
VOLUME /config

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD nc -z 127.0.0.1 6767 || exit 1
