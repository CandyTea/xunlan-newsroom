FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    NEWSROOM_DATA_DIR=/data

WORKDIR /app

RUN addgroup --system --gid 10001 newsroom \
    && adduser --system --uid 10001 --ingroup newsroom --home /home/newsroom newsroom \
    && mkdir -p /data \
    && chown newsroom:newsroom /data

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=newsroom:newsroom app /app/app

VOLUME ["/data"]
EXPOSE 8000
USER newsroom

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
