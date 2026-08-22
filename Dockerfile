# Hugging Face Space (Docker SDK) - the FastAPI enrichment service.
#
# Chromium is not optional here. The keyless search backends answer plain HTTP
# with an anti-bot page, and many manufacturer pages hydrate their spec panel
# client-side, so without a real browser the pipeline retrieves almost nothing.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Chromium + its matching driver from Debian, so Selenium Manager never needs
# to download anything at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        libnss3 \
        libxss1 \
        ca-certificates \
        curl \
    # libasound2 was renamed libasound2t64 in newer Debian; try both, and do not
    # fail the build over an audio library a headless browser never uses.
    && (apt-get install -y --no-install-recommends libasound2t64 \
        || apt-get install -y --no-install-recommends libasound2 \
        || true) \
    && rm -rf /var/lib/apt/lists/* \
    && chromium --version && chromedriver --version

ENV CHROME_BINARY=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Spaces run as uid 1000; everything the app writes must live somewhere it owns.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Writable, ephemeral cache. Spaces restart with a clean filesystem, so this is
# a warm cache within a session rather than durable storage.
ENV CACHE_DIR=/home/user/cache \
    REFERENCE_DIR=/home/user/app/data/reference \
    ENABLE_SELENIUM=true
RUN mkdir -p /home/user/cache /home/user/app/data/reference

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:7860/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--timeout-keep-alive", "120"]
