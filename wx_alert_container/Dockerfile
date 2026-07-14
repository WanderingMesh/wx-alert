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
COPY get_alerts_ntfy.py config.ini /app/

ENTRYPOINT ["python", "/app/get_alerts_ntfy.py", "--config", "/app/config.ini"]
CMD ["--ntfy", "--loop"]
