FROM dhi.io/python:3.13-dev AS build

WORKDIR /app
RUN python -m venv /app/venv

COPY requirements.txt .
RUN /app/venv/bin/pip install \
    --no-cache-dir \
    --requirement requirements.txt

# Create the state directory here rather than in the runtime stage. The
# runtime image is hardened and has no shell, so RUN mkdir is unavailable
# there; COPY --from is the way to get a directory with the right ownership.
#
# 65532 is the nonroot user the Docker Hardened Images run as. The container
# must be able to write its delivery history, or every restart would
# re-announce every active alert.
RUN mkdir -p /stage/data && chown -R 65532:65532 /stage/data


FROM dhi.io/python:3.13

ENV PATH="/app/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=build /app/venv /app/venv
COPY --from=build --chown=65532:65532 /stage/data /data
COPY wx_alert /app/wx_alert
COPY config.ini /app/config.ini

# Declared so that running without an explicit volume still works, though
# state then dies with the container. docker-compose.yml mounts a named
# volume over this, which is what makes the history survive.
VOLUME ["/data"]

# There is no shell or curl in this image, so the check is the application's
# own module. It reads the heartbeat the poll loop writes after each cycle,
# and reports unhealthy when the loop has wedged or a transport has been
# failing. start-period covers the initial serial connect and NWS query.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "wx_alert.health", "/data/heartbeat.json"]

ENTRYPOINT ["python", "-m", "wx_alert", "--config", "/app/config.ini"]
CMD ["--ntfy", "--loop"]
