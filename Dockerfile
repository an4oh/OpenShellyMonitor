FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --system --gid 10001 shelly \
    && useradd --system --uid 10001 --gid 10001 --no-create-home shelly \
    && mkdir -p /data && chown 10001:10001 /data

LABEL org.opencontainers.image.source="https://github.com/an4oh/OpenShellyMonitor" \
      org.opencontainers.image.description="Shelly Cloud ON/OFF time tracker with web dashboard" \
      org.opencontainers.image.licenses="MIT"

ENV SHELLY_DB=/data/shelly-monitor.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
WORKDIR /data
USER 10001:10001
EXPOSE 8787

# Fails while the dashboard is down or the poller has stopped (/healthz answers 503).
# Only meaningful with the default command; `run` alone has no HTTP server.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('SHELLY_HTTP_PORT', '8787'), timeout=4)"]

ENTRYPOINT ["shelly-monitor"]
# One process: poller + dashboard. Set SHELLY_WEB_PASSWORD before exposing the port.
CMD ["serve", "--host", "0.0.0.0", "--poll"]
