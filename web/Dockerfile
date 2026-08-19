FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY web/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY timeline_movie.py .
COPY web/app/ .

ENV DATA_DIR=/data URL_PREFIX=/journey-viewer
VOLUME /data

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
