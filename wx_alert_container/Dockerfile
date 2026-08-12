# Base image notes.
#
# The tag is pinned to the Debian codename rather than left floating. "slim"
# alone would follow Debian to its next stable release and silently change the
# OS under the application; "3.13-slim-trixie" still picks up CPython patch
# releases and Debian security updates on rebuild, which is the tradeoff worth
# having.
#
# No apt packages are installed. Every dependency, including pycryptodome and
# the dbus-fast that bleak pulls in, ships prebuilt manylinux wheels, so the
# runtime needs no compiler and no shared libraries beyond the base.

FROM python:3.13-slim-trixie AS build

WORKDIR /app
RUN python -m venv /app/venv

COPY requirements.txt .
RUN /app/venv/bin/pip install \
    --no-cache-dir \
    --requirement requirements.txt


FROM python:3.13-slim-trixie

ENV PATH="/app/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# A dedicated unprivileged user. The UID is fixed rather than
# auto-assigned because it becomes the owner of the /data volume: a UID that
# drifted between rebuilds would leave the container unable to read back its
# own delivery history, and it would re-announce every active alert.
#
# No home directory and no login shell, since the process needs neither.
RUN groupadd --system --gid 10001 wxalert \
 && useradd --system --uid 10001 --gid wxalert \
      --no-create-home --shell /usr/sbin/nologin wxalert

WORKDIR /app

COPY --from=build /app/venv /app/venv
COPY wx_alert /app/wx_alert
COPY config.ini /app/config.ini

# Created with ownership already set. Docker copies this directory's ownership
# onto a fresh named volume mounted here, which is what lets the unprivileged
# user write state without any entrypoint chown dance.
RUN install -d -o wxalert -g wxalert -m 0755 /data

# Declared so running without an explicit volume still works, though state
# then dies with the container. docker-compose.yml mounts a named volume over
# this, which is what makes the history survive container replacement.
VOLUME ["/data"]

USER wxalert

# The check is the application's own module rather than a shell pipeline or
# curl. It reads the heartbeat the poll loop writes after each cycle and
# reports unhealthy when the loop has wedged or a transport has been failing.
# start-period covers the initial serial connect and first NWS query.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "wx_alert.health", "/data/heartbeat.json"]

ENTRYPOINT ["python", "-m", "wx_alert", "--config", "/app/config.ini"]
CMD ["--ntfy", "--loop"]
