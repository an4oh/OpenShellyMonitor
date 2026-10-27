FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --system --gid 10001 shelly \
    && useradd --system --uid 10001 --gid 10001 --no-create-home shelly \
    && mkdir -p /data && chown 10001:10001 /data

ENV SHELLY_DB=/data/shelly-monitor.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
WORKDIR /data
USER 10001:10001
EXPOSE 8787

ENTRYPOINT ["shelly-monitor"]
CMD ["run"]
