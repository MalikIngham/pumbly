# Development image: runs the FastAPI backend and Next.js frontend together.
FROM node:25.3.0-slim AS node

FROM python:3.14.2-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEXT_TELEMETRY_DISABLED=1

COPY backend/requirements.txt backend/requirements-dev.txt backend/
RUN pip install --no-cache-dir -r backend/requirements-dev.txt

COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci

COPY backend/ backend/
COPY frontend/ frontend/
COPY docker/dev.sh docker/dev.sh

EXPOSE 3000 8000

CMD ["/app/docker/dev.sh"]
