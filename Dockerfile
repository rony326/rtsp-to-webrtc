FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY frontend/ ./frontend/

ENV HTTP_PORT=8080
ENV TCP_PORT=9000
ENV HLS_ROOT=/tmp/hls
ENV CONFIG_PATH=/app/config.json
ENV STATIC_DIR=/app/frontend

EXPOSE 8080 9000

CMD ["python", "main.py"]
