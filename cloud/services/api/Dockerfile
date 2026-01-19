FROM python:3.12-slim

ARG APP_VERSION=dev
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="OVR Cloud Fleet API" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY map_tiles.py ./

EXPOSE 8089
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8089", "app:app"]
