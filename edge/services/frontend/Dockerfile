FROM node:20-alpine AS build

ARG APP_VERSION=dev
ARG GIT_SHA=unknown

# Expose version to Vite at build time (optional)
ENV VITE_APP_VERSION="${APP_VERSION}" \
    VITE_GIT_SHA="${GIT_SHA}"

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine

ARG APP_VERSION=dev
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="OVR Edge Frontend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
