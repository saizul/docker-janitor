FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DOCKER_JANITOR_STATE_FILE=/data/state.json \
    DOCKER_JANITOR_HEARTBEAT=/tmp/docker-janitor.heartbeat
# docker-janitor never removes its own image.
LABEL janitor.keep="true"
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && mkdir -p /data /config \
    && rm -rf /app/build
# Runs as root because the Docker socket is root-owned on most hosts. Access to the
# socket is root-equivalent anyway; see "Least privilege" in the README for a socket proxy.
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD ["docker-janitor", "healthcheck", "--max-age", "300"]
ENTRYPOINT ["docker-janitor"]
CMD ["daemon"]
