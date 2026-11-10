#!/usr/bin/env bash
# Runs both dev servers. If either one exits, the container stops with it.

if [ ! -f /app/backend/.env ] && [ -z "${TUMBLR_CONSUMER_KEY:-}" ]; then
    echo 'backend/.env is missing. Copy backend/.env.example to backend/.env and set TUMBLR_CONSUMER_KEY.' >&2
    exit 1
fi

stop() {
    kill $(jobs -p) 2>/dev/null
}
trap stop TERM INT

(cd /app/backend && exec uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000) &
(cd /app/frontend && exec npm run dev -- --hostname 0.0.0.0 --port 3000) &

wait -n
status=$?
stop
wait
exit $status
