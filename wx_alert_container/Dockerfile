FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system wxalert \
    && useradd --system --gid wxalert --home-dir /app --no-create-home wxalert

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement /app/requirements.txt

COPY --chown=wxalert:wxalert get_alerts_ntfy.py config.ini /app/

USER wxalert

# Default: perform one NWS check, publish active alerts to ntfy, then exit.
# Test mode: docker run --rm IMAGE --ntfy-test
ENTRYPOINT ["python", "/app/get_alerts_ntfy.py", "--config", "/app/config.ini"]
CMD ["--ntfy"]
