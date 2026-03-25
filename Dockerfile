# ── Xenia Shot Controller ─────────────────────────────────────────────────────
# Multi-stage build: slim final image, non-root user, healthcheck included.
#
# Build:  docker build -t xenia-shot-controller .
# Run:    docker run -p 8765:8765 -p 8766:8766 \
#           -v $(pwd)/data:/app/data \
#           -e XENIA_HOST=http://192.168.x.x \
#           xenia-shot-controller

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps for aiohttp's C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Xenia Shot Controller"
LABEL org.opencontainers.image.description="Real-time espresso shot monitor and AI barista coach for the Xenia Dual Boiler"
LABEL org.opencontainers.image.url="https://github.com/simoncharmms/xenia-shot-controller"
LABEL org.opencontainers.image.licenses="MIT"

# Non-root user
RUN addgroup --system xenia && adduser --system --ingroup xenia xenia

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY --chown=xenia:xenia controller.py .
COPY --chown=xenia:xenia ui/ ui/
COPY --chown=xenia:xenia data/config.example.json data/config.example.json

# Data dir (config + shot log) — should be mounted as a volume
RUN mkdir -p data && chown xenia:xenia data

USER xenia

# WebSocket port / HTTP UI port
EXPOSE 8765 8766

# Volume for persistent data (shots.json + config.json)
VOLUME ["/app/data"]

# Healthcheck via the built-in HTTP server
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8766/health')" || exit 1

CMD ["python", "controller.py"]
