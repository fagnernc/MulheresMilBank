FROM python:3.13-slim

WORKDIR /app

COPY app.py .
COPY storage.py .
COPY assets ./assets

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data
ENV PORTA=8000

VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "app.py"]
