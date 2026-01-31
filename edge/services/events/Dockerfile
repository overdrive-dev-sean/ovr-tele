FROM python:3.12-slim

ARG APP_VERSION=dev
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="OVR Edge Event Service" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}"

WORKDIR /app

# Install mosquitto-clients for MQTT-based GX control
RUN apt-get update && apt-get install -y --no-install-recommends mosquitto-clients && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY map_tiles.py ./

EXPOSE 8088
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8088", "app:app"]
