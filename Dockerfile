# syntax=docker/dockerfile:1.7
# ---------- build stage: resolve Python deps with uv ----------
FROM python:3.12-slim-bookworm AS builder

# Install latest uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Resolve & install dependencies into a relocatable venv at /app/.venv.
# We mount the cache from the build host so subsequent builds reuse the
# package download cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

COPY src ./src
COPY package.json ./

# Install the project itself in the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


# ---------- runtime stage ----------
FROM python:3.12-slim-bookworm

# zendriver talks to a real browser via the Chrome DevTools Protocol. We use
# Chromium so we can keep the build self-contained. Xvfb is needed because
# the Cloudflare bypass works best with a non-headless browser; on Linux that
# means running a virtual X server.
#
# The dummy `equivs` packages trick lets us skip the heavy `libgl1-mesa-dri`
# and `adwaita-icon-theme` dependencies that Chromium pulls in but we never use.
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends equivs \
    && equivs-control libgl1-mesa-dri \
    && printf 'Section: misc\nPriority: optional\nStandards-Version: 3.9.2\nPackage: libgl1-mesa-dri\nVersion: 99.0.0\nDescription: Dummy package for libgl1-mesa-dri\n' >> libgl1-mesa-dri \
    && equivs-build libgl1-mesa-dri \
    && mv libgl1-mesa-dri_*.deb /libgl1-mesa-dri.deb \
    && equivs-control adwaita-icon-theme \
    && printf 'Section: misc\nPriority: optional\nStandards-Version: 3.9.2\nPackage: adwaita-icon-theme\nVersion: 99.0.0\nDescription: Dummy package for adwaita-icon-theme\n' >> adwaita-icon-theme \
    && equivs-build adwaita-icon-theme \
    && mv adwaita-icon-theme_*.deb /adwaita-icon-theme.deb

# Install the dummy packages and the real system dependencies.
RUN dpkg -i /libgl1-mesa-dri.deb /adwaita-icon-theme.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium chromium-common chromium-driver \
        xvfb dumb-init procps curl vim xauth ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/lib/x86_64-linux-gnu/libmfxhw* \
    && rm -f /usr/lib/x86_64-linux-gnu/mfx/* \
    && useradd --home-dir /app --shell /bin/sh flaresolverr \
    && mv /usr/bin/chromedriver /usr/local/bin/chromedriver \
    && mkdir /config \
    && chown flaresolverr:flaresolverr /config

# Copy the resolved venv and the application source.
COPY --from=builder --chown=flaresolverr:flaresolverr /app/.venv /app/.venv
COPY --from=builder --chown=flaresolverr:flaresolverr /app/src /app/src
COPY --from=builder --chown=flaresolverr:flaresolverr /app/package.json /app/package.json
COPY --from=builder --chown=flaresolverr:flaresolverr /app/pyproject.toml /app/pyproject.toml

# Make sure the venv binaries are on PATH.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

VOLUME /config
USER flaresolverr

EXPOSE 8191
EXPOSE 8192

# dumb-init avoids zombie chromium processes
ENTRYPOINT ["/usr/bin/dumb-init", "--"]

CMD ["python", "-u", "/app/src/flaresolverr.py"]

# Local build
# docker build -t ngosang/flaresolverr:3.6.0 .
# docker run -p 8191:8191 ngosang/flaresolverr:3.6.0

# Multi-arch build
# docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
# docker buildx create --use
# docker buildx build -t ngosang/flaresolverr:3.6.0 --platform linux/386,linux/amd64,linux/arm/v7,linux/arm64/v8 .
#   add --push to publish in DockerHub

# Test multi-arch build
# docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
# docker buildx create --use
# docker buildx build -t ngosang/flaresolverr:3.6.0 --platform linux/arm/v7 --load .
# docker run -p 8191:8191 --platform linux/arm/v7 ngosang/flaresolverr:3.6.0
