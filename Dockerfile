FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 mulheresmil \
    && useradd --uid 10001 --gid mulheresmil --create-home --shell /usr/sbin/nologin mulheresmil

WORKDIR /app

COPY --chown=10001:10001 app.py .
COPY --chown=10001:10001 storage.py .
COPY --chown=10001:10001 assets ./assets

RUN mkdir /data && chown mulheresmil:mulheresmil /data

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data
ENV PORTA=8000

VOLUME ["/data"]

EXPOSE 8000

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["python", "app.py"]
