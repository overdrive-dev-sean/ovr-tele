# OVR React Frontend (Migration)

This folder contains the React frontend scaffold. The backend API remains in `events`.

## Local dev

From this folder:

```bash
npm install
npm run dev
```

The dev server proxies `/api` to `http://localhost:8088`.

## Build

```bash
npm run build
```

The Docker image builds the app and serves it via Nginx on port 8080 with `/api` proxied to the backend.
