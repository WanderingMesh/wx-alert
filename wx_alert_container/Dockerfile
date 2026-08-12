FROM dhi.io/python:3.13-dev AS build

WORKDIR /app
RUN python -m venv /app/venv

COPY requirements.txt .
RUN /app/venv/bin/pip install \
    --no-cache-dir \
    --requirement requirements.txt


FROM dhi.io/python:3.13

ENV PATH="/app/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=build /app/venv /app/venv
COPY wx_alert /app/wx_alert
COPY config.ini /app/config.ini

ENTRYPOINT ["python", "-m", "wx_alert", "--config", "/app/config.ini"]
CMD ["--ntfy", "--loop"]
